"""Every SMTP connection times out (``EMAIL_TIMEOUT``, config/settings.py).

Django's own default is None - block forever - and the subscription notices go out inside
the billing pass, which holds the scheduler lock while it waits. Minty's Flask twin:
``services/app_runtime/mail.py``.
"""

from django.conf import settings
from django.core.mail import get_connection


def test_the_smtp_backend_is_opened_with_a_timeout():
    assert settings.EMAIL_TIMEOUT == 10.0
    connection = get_connection("django.core.mail.backends.smtp.EmailBackend")
    assert connection.timeout == 10.0
