# -*- coding: utf-8 -*-

# stdlib
from functools import partial
import warnings
from time import time as time_time
from math import ceil

# pypi
from pyramid.exceptions import ConfigurationError
from pyramid.settings import asbool
from redis.exceptions import WatchError
import six
from six import PY2
from six import PY3

# local
from .compat import token_urlsafe


# ---------------------

# create a custom class+object instance for handling lazycreated ids
# this is what dogpile cache's NO_VALUE does
class LazyCreateSession(object):
    pass


LAZYCREATE_SESSION = LazyCreateSession()


# ---------------------


# this stored in the sessions. it is used to detect api version mismatches
SESSION_API_VERSION = 1


def warn_future(message):
    warnings.warn(message, FutureWarning, stacklevel=2)


def to_binary(value, enc="UTF-8"):  # pragma: no cover
    if PY3 and isinstance(value, str):
        value = value.encode(enc)
    return value


def to_unicode(value):  # pragma: no cover
    if PY2:
        value = unicode(value)
    return value


def _generate_session_id():
    """
    Produces a base64 encoded, urlsafe random string with 48-byte
    cryptographically strong randomness as the session id. See

        http://security.stackexchange.com/questions/24850/
        choosing-a-session-id-algorithm-for-a-client-server-relationship

    for the algorithm of choosing a session id.

    The implementation of `os.urandom` varies by system, but you can always
    supply your own function in your ini file with:

        redis.sessions.id_generator = my_random_id_generator

    This uses 48 bytes instead of 32 to maintain backwards
    compatibility to pyramid_redis_sessions.  The earlier packaged used
    a 64character digest; however 48bits using the new method will
    encode to a 64 character url-safe string, while 32 bits will only be encoded
    to a 40 character string.
    """
    return token_urlsafe(48)


# ---------------------

# `_parse_settings` and `includeme` may need to coerce strings into other types
# these lists are maintained here as a public API, so implementers who need
# to customize their integration can do so without fear of breaking changes

configs_dotable = (
    "client_callable",
    "serialize",
    "deserialize",
    "id_generator",
    "func_check_response_allow_cookies",
    "func_invalid_logger",
    "cookie_signer",
)

configs_bool = (
    "cookie_secure",
    "cookie_httponly",
    "cookie_on_exception",
    "set_redis_ttl",
    "set_redis_ttl_readheavy",
    "detect_changes",
    "deserialized_fails_new",
    "python_expires",
)

configs_int = ("port", "db", "cookie_max_age")

configs_int_none = ("timeout", "timeout_trigger")


# ---------------------


def prefixed_id(prefix="session:"):
    """
    Adds a prefix to the unique session id, for cases where you want to
    visually distinguish keys in redis.
    """
    session_id = _generate_session_id()
    prefixed_id = prefix + session_id
    return prefixed_id


def int_time():
    return int(ceil(time_time()))


def empty_session_payload(timeout=0, python_expires=None):
    """creates an empty session payload
    """
    _created = int_time()
    data = {
        "m": {},  # managed_dict
        "c": _created,  # created
        "v": SESSION_API_VERSION,  # session
    }
    if timeout:
        data["t"] = timeout  # timeout
        if python_expires:
            data["x"] = _created + timeout
    return data


def encode_session_payload(
    managed_dict, created, timeout, expires, timeout_trigger=None, python_expires=None
):
    """called by a session to recode for storage;
       inverse of ``decode_session_payload``
    """
    data = {
        "m": managed_dict,  # managed_dict
        "c": created,  # created
        "v": SESSION_API_VERSION,  # session_api version
    }
    if expires and python_expires:
        data["x"] = expires
    if timeout:
        data["t"] = timeout  # timeout
        if python_expires:
            time_now = int_time()
            if not timeout_trigger or (time_now >= (expires - timeout_trigger)):
                data["x"] = time_now + timeout  # expires
    return data


def decode_session_payload(payload):
    """decode a serialized session payload to kwargs
       inverse of ``encode_session_payload``
    """
    return {
        "managed_dict": payload["m"],
        "created": payload["c"],
        "version": payload["v"],
        "timeout": payload.get("t"),
        "expires": payload.get("x"),
    }


def _insert_session_id_if_unique(
    redis,
    timeout,
    session_id,
    serialize,
    set_redis_ttl,
    data_payload=None,
    new_payload_func=None,
    python_expires=None,
):
    """ Attempt to insert a given ``session_id`` and return the successful id
    or ``None``.  ``timeout`` could be 0/None, in that case do-not track
    the timeout data

    This will create an empty/null session and redis entry for the id.

    ``data_payload`` = payload to use
    ``new_payload_func`` = specify a fallback function to generate a payload
    if both are ``None``, then `empty_session_payload`
    """
    if data_payload is None:
        if new_payload_func is not None:
            data_payload = new_payload_func()
        else:
            data_payload = empty_session_payload(timeout, python_expires=python_expires)
    _payload = serialize(data_payload)
    with redis.pipeline() as pipe:
        try:
            # start pipeline with a watch
            pipe.watch(session_id)
            # after `watch` the pipline is in immediate execution mode
            value = pipe.get(session_id)
            if value is not None:
                return None
            # enter buffered mode
            pipe.multi()
            if timeout and set_redis_ttl:
                pipe.setex(session_id, timeout, _payload)
            else:
                pipe.set(session_id, _payload)
            pipe.execute()
            # if a WatchError wasn't raised during execution, everything
            # we just did happened atomically
            return session_id
        except WatchError:
            return None


def create_unique_session_id(
    redis,
    timeout,
    serialize,
    generator=_generate_session_id,
    set_redis_ttl=True,
    data_payload=None,
    new_payload_func=None,
    python_expires=None,
):
    """
    Returns a unique session id after inserting it successfully in Redis.
    """
    while 1:
        session_id = generator()
        attempt = _insert_session_id_if_unique(
            redis,
            timeout,
            session_id,
            serialize,
            set_redis_ttl,
            data_payload=data_payload,
            new_payload_func=new_payload_func,
            python_expires=python_expires,
        )
        if attempt is not None:
            return attempt


def _parse_settings(settings):
    """
    Convenience function to collect settings prefixed by 'redis.sessions' and
    coerce settings to ``int``, ``float``, and ``bool`` as needed.
    """
    keys = [s for s in settings if s.startswith("redis.sessions.")]

    options = {}

    for k in keys:
        param = k.split(".")[-1]
        value = settings[k]
        options[param] = value

    _secret_cookiesigner = (options.get("secret"), options.get("cookie_signer"))
    if all(_secret_cookiesigner) or not any(_secret_cookiesigner):
        raise ConfigurationError(
            "One, and only one, of `redis.sessions.secret` and `redis.sessions.cookie_signer` must be provided."
        )

    # coerce bools
    for b in configs_bool:
        if b in options:
            options[b] = asbool(options[b])

    # coerce ints
    for i in configs_int:
        if i in options:
            options[i] = int(options[i])

    # allow "None" to be a value for some ints
    for i in configs_int_none:
        if i in options:
            if options[i] == "None":
                options[i] = None
            else:
                options[i] = int(options[i])
                if not options[i]:
                    options[i] = None

    # coerce float
    if "socket_timeout" in options:
        options["socket_timeout"] = float(options["socket_timeout"])

    # check for settings conflict
    if "prefix" in options and "id_generator" in options:
        err = "cannot specify custom id_generator and a key prefix"
        raise ConfigurationError(err)

    # convenience setting for overriding key prefixes
    if "prefix" in options:
        prefix = options.pop("prefix")
        options["id_generator"] = partial(prefixed_id, prefix=prefix)

    return options


def refresh(wrapped):
    """
    Decorator to reset the expire time for this session's key in Redis.
    This will mark the `_session_state.please_refresh` as True, to be
    handled in a callback.
    To immediately persist a session, call `session.do_refresh`.
    """

    def wrapped_refresh(session, *arg, **kw):
        result = wrapped(session, *arg, **kw)
        session._session_state.please_refresh = True
        return result

    return wrapped_refresh


def persist(wrapped):
    """
    Decorator to persist in Redis all the data that needs to be persisted for
    this session and reset the expire time.
    This will mark the `_session_state.please_persist` as True, to be
    handled in a callback.
    To immediately persist a session, call `session.do_persist`.
    """

    def wrapped_persist(session, *arg, **kw):
        result = wrapped(session, *arg, **kw)
        session._session_state.please_persist = True
        return result

    return wrapped_persist


class _NullSerializer(object):
    """
    A fake serializer for compatibility with ``webob.cookies.SignedSerializer``.
    Our usage is only signing the session_id, which is a string. 
    """

    def loads(self, data):
        return data

    def dumps(self, data):
        return data
