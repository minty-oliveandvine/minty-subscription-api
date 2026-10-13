"""PREVIEW_ORIGIN_REGEX (config/preview_origin.py): unset is off; a value that could let in
anything but a preview set's https://pr-<slug>-<app>.dailyminty.com origins refuses to boot.

The same file is in minty-subscription-api, minty-payment-request-api and
minty-onboarding-api; change the three together."""

import re

import pytest
from django.core.exceptions import ImproperlyConfigured

from config.preview_origin import preview_origin_regexes

# What minty-infra services.tf writes, narrowed to the apps that call this API.
GOOD = r"^https://pr-[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?-(?:my|payment-request|onboarding)\.dailyminty\.com$"


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_unset_is_off(raw):
    assert preview_origin_regexes(raw) == []


def test_the_terraform_value_is_accepted_and_matches_only_a_set():
    [value] = preview_origin_regexes(f"  {GOOD} ")
    pattern = re.compile(value)
    assert pattern.match("https://pr-feat-x-my.dailyminty.com")
    assert pattern.match("https://pr-feat-x-payment-request.dailyminty.com")
    assert not pattern.match("https://pr-feat-x-www.dailyminty.com")


@pytest.mark.parametrize(
    "raw",
    [
        r".*",
        r"^https://.*$",
        r"https://pr-.*\.dailyminty\.com$",  # no ^ anchor
        r"^https://pr-.*\.dailyminty\.com",  # no $ anchor
        r"^http://pr-.*\.dailyminty\.com$",
        r"^https://pr-.*\.vercel\.app$",
        r"^https://pr-a|.*\.dailyminty\.com$",  # a top-level | escapes both anchors
        r"^https://pr-[a-z]+|evil|x\.dailyminty\.com$",
        r"^https://pr-(.*\.dailyminty\.com$",  # does not compile
    ],
)
def test_a_value_that_could_let_in_another_origin_refuses_to_boot(raw):
    with pytest.raises(ImproperlyConfigured, match="PREVIEW_ORIGIN_REGEX"):
        preview_origin_regexes(raw)


def test_a_bar_inside_a_group_or_class_is_fine():
    assert preview_origin_regexes(r"^https://pr-[a-z|]+-(?:my|onboarding)\.dailyminty\.com$")
