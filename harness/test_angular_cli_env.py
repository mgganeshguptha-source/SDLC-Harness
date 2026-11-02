"""Angular CLI exits 127 on its own unhandled exceptions — not 'command not
found' (petclinic-angular-demo PC-102, 2026-11-02)."""
from validation import classify_environment_failure, environment_hint

NG_CONFIG = ("An unhandled exception occurred: Configuration 'development' for target "
             "'build' in project 'spring-petclinic-angular' is not set in the workspace.")


def test_missing_angular_configuration():
    reason = classify_environment_failure(127, "npm warn deprecated x\n" + NG_CONFIG)
    assert reason == "angular_config"
    hint = environment_hint(reason)
    assert "angular.json" in hint and "Maven" not in hint


def test_other_angular_cli_exception():
    assert classify_environment_failure(127, "An unhandled exception occurred: boom") == "angular_cli"


def test_real_command_not_found_still_toolchain():
    assert classify_environment_failure(127, "bash: ./mvnw: No such file or directory") == "toolchain"
