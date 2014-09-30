# Copyright 2011 OpenStack Foundation.
# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""OpenStack logging handler.

This module adds to logging functionality by adding the option to specify
a context object when calling the various log methods.  If the context object
is not specified, default formatting is used. Additionally, an instance uuid
may be passed as part of the log message, which is intended to make it easier
for admins to find messages related to a specific instance.

It also allows setting of formatting information through conf.

"""

import logging
import logging.config
import logging.handlers
import os
import socket
import sys
import traceback

from oslo.config import cfg
from oslo.utils import importutils
import six
from six import moves

_PY26 = sys.version_info[0:2] == (2, 6)

from oslo.log._i18n import _
from oslo.log import _local
from oslo.log import _options
from oslo.log import context as ctx
from oslo.log import formatters
from oslo.log import handlers

# our new audit level
# NOTE(jkoelker) Since we synthesized an audit level, make the logging
#                module aware of it so it acts like other levels.
logging.AUDIT = logging.INFO + 1
logging.addLevelName(logging.AUDIT, 'AUDIT')


def _get_log_file_path(conf, binary=None):
    logfile = conf.log_file
    logdir = conf.log_dir

    if logfile and not logdir:
        return logfile

    if logfile and logdir:
        return os.path.join(logdir, logfile)

    if logdir:
        binary = binary or handlers._get_binary_name()
        return '%s.log' % (os.path.join(logdir, binary),)

    return None


class BaseLoggerAdapter(logging.LoggerAdapter):

    warn = logging.LoggerAdapter.warning

    @property
    def handlers(self):
        return self.logger.handlers

    def audit(self, msg, *args, **kwargs):
        self.log(logging.AUDIT, msg, *args, **kwargs)

    def isEnabledFor(self, level):
        if _PY26:
            # This method was added in python 2.7 (and it does the exact
            # same logic, so we need to do the exact same logic so that
            # python 2.6 has this capability as well).
            return self.logger.isEnabledFor(level)
        else:
            return super(BaseLoggerAdapter, self).isEnabledFor(level)


class LazyAdapter(BaseLoggerAdapter):
    def __init__(self, name='unknown', version='unknown'):
        self._logger = None
        self.extra = {}
        self.name = name
        self.version = version

    @property
    def logger(self):
        if not self._logger:
            self._logger = getLogger(self.name, self.version)
            if six.PY3:
                # In Python 3, the code fails because the 'manager' attribute
                # cannot be found when using a LoggerAdapter as the
                # underlying logger. Work around this issue.
                self._logger.manager = self._logger.logger.manager
        return self._logger


class KeywordArgumentAdapter(BaseLoggerAdapter):
    """Logger adapter to add keyword arguments to log record's extra data
    """

    def process(self, msg, kwargs):
        # Make a new extra dictionary combining the values we were
        # given when we were constructed and anything from kwargs.
        extra = {}
        extra.update(self.extra)
        if 'extra' in kwargs:
            extra.update(kwargs.pop('extra'))
        # Move any unknown keyword arguments into the extra
        # dictionary.
        for name in list(kwargs.keys()):
            if name == 'exc_info':
                continue
            extra[name] = kwargs.pop(name)
        # Place the updated extra values back into the keyword
        # arguments.
        kwargs['extra'] = extra
        return msg, kwargs


class ContextAdapter(BaseLoggerAdapter):
    warn = logging.LoggerAdapter.warning

    def __init__(self, logger, project_name, version_string):
        self.logger = logger
        self.project = project_name
        self.version = version_string
        self._deprecated_messages_sent = dict()

    @property
    def handlers(self):
        return self.logger.handlers

    def deprecated(self, msg, *args, **kwargs):
        """Call this method when a deprecated feature is used.

        If the system is configured for fatal deprecations then the message
        is logged at the 'critical' level and :class:`DeprecatedConfig` will
        be raised.

        Otherwise, the message will be logged (once) at the 'warn' level.

        :raises: :class:`DeprecatedConfig` if the system is configured for
                 fatal deprecations.

        """
        stdmsg = _("Deprecated: %s") % msg
        if ctx._config.fatal_deprecations:
            self.critical(stdmsg, *args, **kwargs)
            raise DeprecatedConfig(msg=stdmsg)

        # Using a list because a tuple with dict can't be stored in a set.
        sent_args = self._deprecated_messages_sent.setdefault(msg, list())

        if args in sent_args:
            # Already logged this message, so don't log it again.
            return

        sent_args.append(args)
        self.warn(stdmsg, *args, **kwargs)

    def process(self, msg, kwargs):
        # NOTE(jecarey): If msg is not unicode, coerce it into unicode
        #                before it can get to the python logging and
        #                possibly cause string encoding trouble
        if not isinstance(msg, six.text_type):
            msg = six.text_type(msg)

        if 'extra' not in kwargs:
            kwargs['extra'] = {}
        extra = kwargs['extra']

        context = kwargs.pop('context', None)
        if not context:
            context = getattr(_local.store, 'context', None)
        if context:
            extra.update(formatters._dictify_context(context))

        instance = kwargs.pop('instance', None)
        instance_uuid = (extra.get('instance_uuid') or
                         kwargs.pop('instance_uuid', None))
        instance_extra = ''
        if instance:
            instance_extra = ctx._config.instance_format % instance
        elif instance_uuid:
            instance_extra = (ctx._config.instance_uuid_format
                              % {'uuid': instance_uuid})
        extra['instance'] = instance_extra

        extra.setdefault('user_identity', kwargs.pop('user_identity', None))

        extra['project'] = self.project
        extra['version'] = self.version
        extra['extra'] = extra.copy()
        return msg, kwargs


def _create_logging_excepthook(product_name):
    def logging_excepthook(exc_type, value, tb):
        extra = {'exc_info': (exc_type, value, tb)}
        getLogger(product_name).critical(
            "".join(traceback.format_exception_only(exc_type, value)),
            **extra)
    return logging_excepthook


class LogConfigError(Exception):

    message = _('Error loading logging config %(log_config)s: %(err_msg)s')

    def __init__(self, log_config, err_msg):
        self.log_config = log_config
        self.err_msg = err_msg

    def __str__(self):
        return self.message % dict(log_config=self.log_config,
                                   err_msg=self.err_msg)


def _load_log_config(log_config_append):
    try:
        logging.config.fileConfig(log_config_append,
                                  disable_existing_loggers=False)
    except (moves.configparser.Error, KeyError) as exc:
        raise LogConfigError(log_config_append, six.text_type(exc))


def register_options(conf):
    # NOTE(dims): We need this global variable until we
    # deprecate ContextAdapter and ContextFormatter
    ctx._config = conf

    conf.register_cli_opts(_options.common_cli_opts)
    conf.register_cli_opts(_options.logging_cli_opts)
    conf.register_opts(_options.generic_log_opts)
    conf.register_opts(_options.log_opts)


def setup(conf, product_name, version='unknown'):
    """Setup logging."""
    if conf.log_config_append:
        _load_log_config(conf.log_config_append)
    else:
        _setup_logging_from_conf(conf, product_name, version)
    sys.excepthook = _create_logging_excepthook(product_name)


def set_defaults(logging_context_format_string=None,
                 default_log_levels=None):
    # Just in case the caller is not setting the
    # default_log_level. This is insurance because
    # we introduced the default_log_level parameter
    # later in a backwards in-compatible change
    if default_log_levels is not None:
        cfg.set_defaults(
            _options.log_opts,
            default_log_levels=default_log_levels)
    if logging_context_format_string is not None:
        cfg.set_defaults(
            _options.log_opts,
            logging_context_format_string=logging_context_format_string)


def _find_facility_from_conf(conf):
    facility_names = logging.handlers.SysLogHandler.facility_names
    facility = getattr(logging.handlers.SysLogHandler,
                       conf.syslog_log_facility,
                       None)

    if facility is None and conf.syslog_log_facility in facility_names:
        facility = facility_names.get(conf.syslog_log_facility)

    if facility is None:
        valid_facilities = facility_names.keys()
        consts = ['LOG_AUTH', 'LOG_AUTHPRIV', 'LOG_CRON', 'LOG_DAEMON',
                  'LOG_FTP', 'LOG_KERN', 'LOG_LPR', 'LOG_MAIL', 'LOG_NEWS',
                  'LOG_AUTH', 'LOG_SYSLOG', 'LOG_USER', 'LOG_UUCP',
                  'LOG_LOCAL0', 'LOG_LOCAL1', 'LOG_LOCAL2', 'LOG_LOCAL3',
                  'LOG_LOCAL4', 'LOG_LOCAL5', 'LOG_LOCAL6', 'LOG_LOCAL7']
        valid_facilities.extend(consts)
        raise TypeError(_('syslog facility must be one of: %s') %
                        ', '.join("'%s'" % fac
                                  for fac in valid_facilities))

    return facility


def _setup_logging_from_conf(conf, project, version):
    log_root = getLogger(None).logger
    for handler in log_root.handlers:
        log_root.removeHandler(handler)

    logpath = _get_log_file_path(conf)
    if logpath:
        filelog = logging.handlers.WatchedFileHandler(logpath)
        log_root.addHandler(filelog)

    if conf.use_stderr:
        streamlog = handlers.ColorHandler()
        log_root.addHandler(streamlog)

    elif not logpath:
        # pass sys.stdout as a positional argument
        # python2.6 calls the argument strm, in 2.7 it's stream
        streamlog = logging.StreamHandler(sys.stdout)
        log_root.addHandler(streamlog)

    if conf.publish_errors:
        handler = importutils.import_object(
            "oslo.messaging.notify.log_handler.PublishErrorsHandler",
            logging.ERROR)
        log_root.addHandler(handler)

    datefmt = conf.log_date_format
    for handler in log_root.handlers:
        # NOTE(alaski): CONF.log_format overrides everything currently.  This
        # should be deprecated in favor of context aware formatting.
        if conf.log_format:
            handler.setFormatter(logging.Formatter(fmt=conf.log_format,
                                                   datefmt=datefmt))
            log_root.info('Deprecated: log_format is now deprecated and will '
                          'be removed in the next release')
        else:
            handler.setFormatter(formatters.ContextFormatter(project=project,
                                                             version=version,
                                                             datefmt=datefmt,
                                                             config=conf))

    if conf.debug:
        log_root.setLevel(logging.DEBUG)
    elif conf.verbose:
        log_root.setLevel(logging.INFO)
    else:
        log_root.setLevel(logging.WARNING)

    for pair in conf.default_log_levels:
        mod, _sep, level_name = pair.partition('=')
        logger = logging.getLogger(mod)
        # NOTE(AAzza) in python2.6 Logger.setLevel doesn't convert string name
        # to integer code.
        if sys.version_info < (2, 7):
            level = logging.getLevelName(level_name)
            logger.setLevel(level)
        else:
            logger.setLevel(level_name)

    if conf.use_syslog:
        try:
            facility = _find_facility_from_conf(conf)
            # TODO(bogdando) use the format provided by RFCSysLogHandler
            #   after existing syslog format deprecation in J
            if conf.use_syslog_rfc_format:
                syslog = handlers.RFCSysLogHandler(facility=facility)
            else:
                syslog = logging.handlers.SysLogHandler(facility=facility)
            log_root.addHandler(syslog)
        except socket.error:
            log_root.error('Unable to add syslog handler. Verify that syslog '
                           'is running.')


_loggers = {}


def getLogger(name='unknown', version='unknown'):
    if name not in _loggers:
        _loggers[name] = ContextAdapter(logging.getLogger(name),
                                        name,
                                        version)
    return _loggers[name]


def getLazyLogger(name='unknown', version='unknown'):
    """Returns lazy logger.

    Creates a pass-through logger that does not create the real logger
    until it is really needed and delegates all calls to the real logger
    once it is created.
    """
    return LazyAdapter(name, version)


class DeprecatedConfig(Exception):
    message = _("Fatal call to deprecated config: %(msg)s")

    def __init__(self, msg):
        super(Exception, self).__init__(self.message % dict(msg=msg))
