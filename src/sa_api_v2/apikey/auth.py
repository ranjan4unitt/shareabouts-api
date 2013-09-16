from django.contrib.auth import login
from django.core.exceptions import PermissionDenied
from rest_framework import authentication
from .models import ApiKey

KEY_HEADER = 'HTTP_X_SHAREABOUTS_KEY'


class APIKeyBackend(object):
    """
    Django authentication backend purely by API key.
    """

    # Not sure yet if we really want to use this as an auth backend;
    # we certainly don't want it in the global settings.AUTHENTICATION_BACKENDS
    supports_object_permissions = False
    supports_anonymous_user = False
    supports_inactive_user = False

    # This needs to be importable.
    backend_name = 'sa_api_v2.apikey.auth.APIKeyBackend'
    model = ApiKey

    def authenticate(self, key=None, ip_address=None):
        if not key:
            return None

        user, key_instance = self._get_user_and_key(key)
        if None in (user, key_instance):
            return None
        key_instance.login(ip_address)
        self.key_instance = key_instance
        return user

    def get_user(self, user_id):
        """user_id is actually an API key.
        """
        return self._get_user_and_key(user_id)[1]

    def _get_user_and_key(self, key):
        try:
            key_instance = self.model.objects.select_related('user').get(key=key)
        except self.model.DoesNotExist:
            return (None, None)
        return key_instance.user, key_instance


def check_api_authorization(request):
    """
    Check API access based on the current request.

    Currently requires that either the user is logged in (eg. via
    basic auth or cookie), or there is a valid API key in the '%s' request
    header.  If either fails, raises ``PermissionDenied``.

    This should become more configurable.
    """ % KEY_HEADER
    ip_address = request.META['REMOTE_ADDR']
    key = request.META.get(KEY_HEADER)
    
    auth_backend = APIKeyBackend()
    user = auth_backend.authenticate(key=key, ip_address=ip_address)
    auth = auth_backend.key_instance if (user is not None) else None
    
    if user is None:
        raise PermissionDenied("invalid key?")
        
    if user.is_active:
        user.backend = APIKeyBackend.backend_name
        return (user, auth)
    else:
        raise PermissionDenied("Your account is disabled.")


class ApiKeyAuthentication(authentication.BaseAuthentication):

    def authenticate(self, request):
        """
        Return a User, or something usable as such, or None;
        as per http://django-rest-framework.org/library/authentication.html

        This wraps check_api_authorization() in something usable
        by djangorestframework.
        """
        try:
            user, auth = check_api_authorization(request)
        except PermissionDenied:
            # Does djrf allow you to provide a message with auth failures?
            return None

        return (user, auth)