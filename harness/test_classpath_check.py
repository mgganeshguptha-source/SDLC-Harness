"""The classpath check that keeps a false MANUAL CHANGE REQUIRED halt from
stopping a run (OP018-6434, 2026-11-19): the plan's External Classes table is
the planning model's judgement from one pom.xml, so a class inherited from a
parent POM or pulled in transitively was reported UNKNOWN and halted the run
twice with nothing for a person to fix."""
import zipfile
from pathlib import Path

import pytest

import classpath_check as CP
from plan_check import ExternalClass

CXF = "org.apache.cxf.jaxws.JaxWsProxyFactoryBean"
TEST_CMD = "mvn -U -T 1C -s $MAVEN_SETTINGS -e test"


def quiet(*a, **k):
    pass


class Cfg:
    def __init__(self, cmd=TEST_CMD, module="app", timeout=900):
        self._cmd, self.target_module, self.test_timeout = cmd, module, timeout

    def resolved_test_command(self):
        return self._cmd


def _jar(path: Path, *entries: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for e in entries:
            z.writestr(e, "")
    return path


def _harness_dir(tmp_path, runs_build=True):
    hd = tmp_path / ".harness"
    hd.mkdir(exist_ok=True)
    (hd / "config.yaml").write_text(f"test_command: x\nruns_build: {str(runs_build).lower()}\n")
    return hd


# ------------------------------------------------------------ command shape
def test_command_keeps_the_settings_file_and_drops_the_test_goal():
    cmd = CP.derive_command(TEST_CMD, "app", Path("/tmp/cp.txt"))
    assert "-s $MAVEN_SETTINGS" in cmd and " test" not in f" {cmd} "
    assert "-pl app" in cmd and "build-classpath" in cmd
    assert "-Dmdep.outputFile=/tmp/cp.txt" in cmd
    assert "-T" not in cmd.split()          # parallel build flag dropped with its value


def test_single_module_repo_gets_no_pl():
    assert "-pl" not in CP.derive_command("mvn test", "", Path("/tmp/cp.txt")).split()


@pytest.mark.parametrize("cmd", ["npm test", "mvn test && ./check.sh", "", "ng test"])
def test_commands_that_are_not_one_plain_maven_call_are_refused(cmd):
    assert CP.derive_command(cmd, "app", Path("/tmp/cp.txt")) is None


# ------------------------------------------------------------- class lookup
def test_class_is_found_inside_a_jar(tmp_path):
    jar = _jar(tmp_path / "cxf.jar", "org/apache/cxf/jaxws/JaxWsProxyFactoryBean.class")
    assert CP.present([jar], CXF) is True
    assert CP.present([jar], "org.apache.cxf.nope.Missing") is False


def test_class_is_found_in_a_directory_entry(tmp_path):
    d = tmp_path / "classes/org/apache/cxf/jaxws"
    d.mkdir(parents=True)
    (d / "JaxWsProxyFactoryBean.class").write_text("")
    assert CP.present([tmp_path / "classes"], CXF) is True


def test_nested_class_is_found_under_its_dollar_name(tmp_path):
    jar = _jar(tmp_path / "a.jar", "org/apache/cxf/Outer$Inner.class")
    assert CP.present([jar], "org.apache.cxf.Outer.Inner") is True


def test_a_broken_or_missing_entry_does_not_raise(tmp_path):
    bad = tmp_path / "broken.jar"
    bad.write_text("not a zip")
    assert CP.present([bad, tmp_path / "gone.jar"], CXF) is False


@pytest.mark.parametrize("name", ["", "   ", "not a class name", "org.apache..x"])
def test_a_name_that_is_not_a_class_is_never_present(tmp_path, name):
    assert CP.present([_jar(tmp_path / "a.jar", "x.class")], name) is False


# ------------------------------------------------------------------ verify
def test_a_class_on_the_classpath_is_cleared_and_the_run_continues(tmp_path, monkeypatch):
    _harness_dir(tmp_path)
    jar = _jar(tmp_path / "libs/cxf.jar", "org/apache/cxf/jaxws/JaxWsProxyFactoryBean.class")
    monkeypatch.setattr(CP, "classpath_entries", lambda *a, **k: [jar])
    res = CP.verify([ExternalClass(CXF, "UNKNOWN")], tmp_path, Cfg(), log=quiet)
    assert res.still_flagged == [] and res.cleared == [CXF] and res.checked is True
    assert "were found and ignored" in CP.evidence_note(res)


def test_a_class_that_is_really_absent_still_halts(tmp_path, monkeypatch):
    _harness_dir(tmp_path)
    jar = _jar(tmp_path / "libs/other.jar", "org/other/Thing.class")
    monkeypatch.setattr(CP, "classpath_entries", lambda *a, **k: [jar])
    flagged = [ExternalClass(CXF, "UNKNOWN")]
    res = CP.verify(flagged, tmp_path, Cfg(), log=quiet)
    assert res.still_flagged == flagged and res.cleared == []
    assert "confirmed 1 class(es) are not on it" in CP.evidence_note(res)


def test_a_mixed_table_halts_only_on_what_is_missing(tmp_path, monkeypatch):
    _harness_dir(tmp_path)
    jar = _jar(tmp_path / "libs/cxf.jar", "org/apache/cxf/jaxws/JaxWsProxyFactoryBean.class")
    monkeypatch.setattr(CP, "classpath_entries", lambda *a, **k: [jar])
    gone = ExternalClass("com.acme.NotThere", "NEW_DEPENDENCY")
    res = CP.verify([ExternalClass(CXF, "UNKNOWN"), gone], tmp_path, Cfg(), log=quiet)
    assert res.still_flagged == [gone] and res.cleared == [CXF]


def test_copy_from_reference_is_never_cleared_by_the_classpath(tmp_path, monkeypatch):
    _harness_dir(tmp_path)
    jar = _jar(tmp_path / "libs/cxf.jar", "org/apache/cxf/jaxws/JaxWsProxyFactoryBean.class")
    monkeypatch.setattr(CP, "classpath_entries", lambda *a, **k: [jar])
    row = ExternalClass(CXF, "COPY_FROM_REFERENCE", "repo@main:src/x")
    res = CP.verify([row], tmp_path, Cfg(), log=quiet)
    assert res.still_flagged == [row] and res.cleared == []
    assert res.checked is False          # nothing to check, Maven never ran


def test_nothing_is_cleared_when_the_classpath_cannot_be_resolved(tmp_path, monkeypatch):
    _harness_dir(tmp_path)
    monkeypatch.setattr(CP, "classpath_entries", lambda *a, **k: None)
    flagged = [ExternalClass(CXF, "UNKNOWN")]
    res = CP.verify(flagged, tmp_path, Cfg(), log=quiet)
    assert res.still_flagged == flagged and res.checked is False
    assert "could not resolve the classpath" in CP.evidence_note(res)


def test_the_check_is_skipped_when_runs_build_is_false(tmp_path, monkeypatch):
    _harness_dir(tmp_path, runs_build=False)
    monkeypatch.setattr(CP, "classpath_entries",
                        lambda *a, **k: pytest.fail("Maven must not run"))
    flagged = [ExternalClass(CXF, "UNKNOWN")]
    res = CP.verify(flagged, tmp_path, Cfg(), log=quiet)
    assert res.still_flagged == flagged and res.checked is False


def test_a_maven_failure_leaves_the_halt_in_place(tmp_path):
    _harness_dir(tmp_path)
    # No pom, no Maven on PATH in CI: the command cannot produce a classpath.
    res = CP.verify([ExternalClass(CXF, "UNKNOWN")], tmp_path,
                    Cfg(cmd="mvn -s $MAVEN_SETTINGS test", timeout=20), log=quiet)
    assert res.checked is False and len(res.still_flagged) == 1


# ------------------------------------------------------------ halt message
def test_the_halt_message_stops_calling_it_a_guess_once_checked():
    from blocked import BlockedDeclaration, halt_message
    d = BlockedDeclaration(declared=True, missing=CXF, file="app/pom.xml")
    checked = halt_message("F-1", "prompt_steps", d,
                           verified="The harness resolved the module's real classpath "
                                    "and confirmed 1 class(es) are not on it.")
    assert "resolved the module's real classpath" in checked
    assert "not a confirmed fact" not in checked
    assert "GENERATES (wsdl2java" in checked
    assert "not a confirmed fact" in halt_message("F-1", "prompt_steps", d)


def test_the_verified_note_is_wrapped_into_the_box():
    from blocked import BlockedDeclaration, halt_message
    long = "The harness resolved the classpath. " + "word " * 60
    m = halt_message("F-1", "prompt_steps", BlockedDeclaration(declared=True), verified=long)
    assert all(len(ln) <= 100 for ln in m.splitlines())
