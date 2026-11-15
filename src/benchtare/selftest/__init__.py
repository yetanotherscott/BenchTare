"""The self-test: the bench check and the harness check. See ``checks``."""

from benchtare.selftest.checks import (
    CheckResult,
    SelfTestResult,
    could_not_open,
    run_selftest,
)

__all__ = ["CheckResult", "SelfTestResult", "could_not_open", "run_selftest"]
