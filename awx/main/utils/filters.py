import re
from functools import reduce
from pyparsing import (
    infixNotation,
    opAssoc,
    Optional,
    Literal,
    CharsNotIn,
    ParseException,
)
from logging import Filter, _levelNames

import six

from django.apps import apps
from django.db import models
from django.conf import settings

from awx.main.utils.common import get_search_fields

__all__ = ['SmartFilter', 'ExternalLoggerEnabled']


class FieldFromSettings(object):
    """
    Field interface - defaults to getting value from setting
    if otherwise set, provided value will take precedence
        over value in settings
    """

    def __init__(self, setting_name):
        self.setting_name = setting_name

    def __get__(self, instance, type=None):
        if self.setting_name in getattr(instance, 'settings_override', {}):
            return instance.settings_override[self.setting_name]
        return getattr(settings, self.setting_name, None)

    def __set__(self, instance, value):
        if value is None:
            if hasattr(instance, 'settings_override'):
                instance.settings_override.pop('instance', None)
        else:
            if not hasattr(instance, 'settings_override'):
                instance.settings_override = {}
            instance.settings_override[self.setting_name] = value


class ExternalLoggerEnabled(Filter):

    # Prevents recursive logging loops from swamping the server
    LOGGER_BLACKLIST = (
        # loggers that may be called in process of emitting a log
        'awx.main.utils.handlers',
        'awx.main.utils.formatters',
        'awx.main.utils.filters',
        'awx.main.utils.encryption',
        'awx.main.utils.log',
        # loggers that may be called getting logging settings
        'awx.conf'
    )

    lvl = FieldFromSettings('LOG_AGGREGATOR_LEVEL')
    enabled_loggers = FieldFromSettings('LOG_AGGREGATOR_LOGGERS')
    enabled_flag = FieldFromSettings('LOG_AGGREGATOR_ENABLED')

    def __init__(self, **kwargs):
        super(ExternalLoggerEnabled, self).__init__()
        for field_name, field_value in kwargs.items():
            if not isinstance(ExternalLoggerEnabled.__dict__.get(field_name, None), FieldFromSettings):
                raise Exception('%s is not a valid kwarg' % field_name)
            if field_value is None:
                continue
            setattr(self, field_name, field_value)

    def filter(self, record):
        """
        Uses the database settings to determine if the current
        external log configuration says that this particular record
        should be sent to the external log aggregator

        False - should not be logged
        True - should be logged
        """
        # Logger exceptions
        for logger_name in self.LOGGER_BLACKLIST:
            if record.name.startswith(logger_name):
                return False
        # General enablement
        if not self.enabled_flag:
            return False

        # Level enablement
        if record.levelno < _levelNames[self.lvl]:
            # logging._levelNames -> logging._nameToLevel in python 3
            return False

        # Logger type enablement
        loggers = self.enabled_loggers
        if not loggers:
            return False
        if record.name.startswith('awx.analytics'):
            base_path, headline_name = record.name.rsplit('.', 1)
            return bool(headline_name in loggers)
        else:
            if '.' in record.name:
                base_name, trailing_path = record.name.split('.', 1)
            else:
                base_name = record.name
            return bool(base_name in loggers)


def string_to_type(t):
    if t == u'null':
        return None
    if t == u'true':
        return True
    elif t == u'false':
        return False

    if re.search('^[-+]?[0-9]+$',t):
        return int(t)

    if re.search('^[-+]?[0-9]+\.[0-9]+$',t):
        return float(t)

    return t


def get_model(name):
    return apps.get_model('main', name)


class SmartFilter(object):
    SEARCHABLE_RELATIONSHIP = 'ansible_facts'

    class BoolOperand(object):
        def __init__(self, t):
            kwargs = dict()
            k, v = self._extract_key_value(t)
            k, v = self._json_path_to_contains(k, v)

            Host = get_model('host')
            search_kwargs = self._expand_search(k, v)
            if search_kwargs:
                kwargs.update(search_kwargs)
                q = reduce(lambda x, y: x | y, [models.Q(**{u'%s__icontains' % _k:_v}) for _k, _v in kwargs.items()])
                self.result = Host.objects.filter(q)
            else:
                # detect loops and restrict access to sensitive fields
                # this import is intentional here to avoid a circular import
                from awx.api.filters import FieldLookupBackend
                FieldLookupBackend().get_field_from_lookup(Host, k)
                kwargs[k] = v
                self.result = Host.objects.filter(**kwargs)

        def strip_quotes_traditional_logic(self, v):
            if type(v) is six.text_type and v.startswith('"') and v.endswith('"'):
                return v[1:-1]
            return v

        def strip_quotes_json_logic(self, v):
            if type(v) is six.text_type and v.startswith('"') and v.endswith('"') and v != u'"null"':
                return v[1:-1]
            return v

        '''
        TODO: We should be able to express this in the grammar and let
              pyparsing do the heavy lifting.
        TODO: separate django filter requests from our custom json filter
              request so we don't process the key any. This could be
              accomplished using a whitelist or introspecting the
              relationship refered to to see if it's a jsonb type.
        '''
        def _json_path_to_contains(self, k, v):
            if not k.startswith(SmartFilter.SEARCHABLE_RELATIONSHIP):
                v = self.strip_quotes_traditional_logic(v)
                return (k, v)

            # Strip off leading relationship key
            if k.startswith(SmartFilter.SEARCHABLE_RELATIONSHIP + '__'):
                strip_len = len(SmartFilter.SEARCHABLE_RELATIONSHIP) + 2
            else:
                strip_len = len(SmartFilter.SEARCHABLE_RELATIONSHIP)
            k = k[strip_len:]

            pieces = k.split(u'__')

            assembled_k = u'%s__contains' % (SmartFilter.SEARCHABLE_RELATIONSHIP)
            assembled_v = None

            last_v = None
            last_kv = None

            for i, piece in enumerate(pieces):
                new_kv = dict()
                if piece.endswith(u'[]'):
                    new_v = []
                    new_kv[piece[0:-2]] = new_v
                else:
                    new_v = dict()
                    new_kv[piece] = new_v

                if last_kv is None:
                    assembled_v = new_kv
                elif type(last_v) is list:
                    last_v.append(new_kv)
                elif type(last_v) is dict:
                    last_kv[last_kv.keys()[0]] = new_kv

                last_v = new_v
                last_kv = new_kv

            v = self.strip_quotes_json_logic(v)

            if type(last_v) is list:
                last_v.append(v)
            elif type(last_v) is dict:
                last_kv[last_kv.keys()[0]] = v

            return (assembled_k, assembled_v)

        def _extract_key_value(self, t):
            t_len = len(t)

            k = None
            v = None

            # key
            # "something"=
            v_offset = 2
            if t_len >= 2 and t[0] == "\"" and t[2] == "\"":
                k = t[1]
                v_offset = 4
            # something=
            else:
                k = t[0]

            # value
            # ="something"
            if t_len > (v_offset + 2) and t[v_offset] == "\"" and t[v_offset + 2] == "\"":
                v = u'"' + six.text_type(t[v_offset + 1]) + u'"'
                #v = t[v_offset + 1]
            # empty ""
            elif t_len > (v_offset + 1):
                v = u""
            # no ""
            else:
                v = string_to_type(t[v_offset])

            return (k, v)

        def _expand_search(self, k, v):
            if 'search' not in k:
                return None

            model, relation = None, None
            if k == 'search':
                model = get_model('host')
            elif k.endswith('__search'):
                relation = k.split('__')[0]
                try:
                    model = get_model(relation)
                except LookupError:
                    raise ParseException('No related field named %s' % relation)

            search_kwargs = {}
            if model is not None:
                search_fields = get_search_fields(model)
                for field in search_fields:
                    if relation is not None:
                        k = '{0}__{1}'.format(relation, field)
                    else:
                        k = field
                    search_kwargs[k] = v
            return search_kwargs


    class BoolBinOp(object):
        def __init__(self, t):
            self.result = None
            i = 2
            while i < len(t[0]):
                if not self.result:
                    self.result = t[0][0].result
                right = t[0][i].result
                self.result = self.execute_logic(self.result, right)
                i += 2


    class BoolAnd(BoolBinOp):
        def execute_logic(self, left, right):
            return left & right


    class BoolOr(BoolBinOp):
        def execute_logic(self, left, right):
            return left | right


    @classmethod
    def query_from_string(cls, filter_string):

        '''
        TODO:
        * handle values with " via: a.b.c.d="hello\"world"
        * handle keys with " via: a.\"b.c="yeah"
        * handle key with __ in it
        '''
        filter_string_raw = filter_string
        filter_string = six.text_type(filter_string)

        unicode_spaces = list(set(six.text_type(c) for c in filter_string if c.isspace()))
        unicode_spaces_other = unicode_spaces + [u'(', u')', u'=', u'"']
        atom = CharsNotIn(unicode_spaces_other)
        atom_inside_quotes = CharsNotIn(u'"')
        atom_quoted = Literal('"') + Optional(atom_inside_quotes) + Literal('"')
        EQUAL = Literal('=')

        grammar = ((atom_quoted | atom) + EQUAL + Optional((atom_quoted | atom)))
        grammar.setParseAction(cls.BoolOperand)

        boolExpr = infixNotation(grammar, [
            ("and", 2, opAssoc.LEFT, cls.BoolAnd),
            ("or",  2, opAssoc.LEFT, cls.BoolOr),
        ])

        try:
            res = boolExpr.parseString('(' + filter_string + ')')
        except ParseException:
            raise RuntimeError(u"Invalid query %s" % filter_string_raw)

        if len(res) > 0:
            return res[0].result

        raise RuntimeError("Parsing the filter_string %s went terribly wrong" % filter_string)
