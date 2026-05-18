from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter


class NoSignupAccountAdapter(DefaultAccountAdapter):
    """Account adapter that closes site-wide signup.

    Acta v0.1.0 is an internal tool with ~20 known users. Open public
    registration would invite spam and fake accounts. Admins create
    accounts through Django admin until an invitation flow is built.
    """

    def is_open_for_signup(self, request):
        """Return False so allauth's signup view renders the closed page.

        Args:
            request: The current HttpRequest.

        Returns:
            Always False — signup is disabled.
        """
        return False


class NoSignupSocialAccountAdapter(DefaultSocialAccountAdapter):
    """Social-login adapter that also blocks first-time signup.

    Mirrors ``NoSignupAccountAdapter`` for the social provider flow so
    a Google login from a stranger cannot create a new local account.
    Existing users with a linked social account still log in normally.
    """

    def is_open_for_signup(self, request, sociallogin):
        """Return False so unknown social logins do not auto-create users.

        Args:
            request: The current HttpRequest.
            sociallogin: The :class:`allauth.socialaccount.models.SocialLogin`
                instance describing the in-flight social auth.

        Returns:
            Always False — signup via social provider is disabled.
        """
        return False
