"""Missing Maven artifacts: the halt names each one and the library-store
folder it belongs in, and no longer points at scripts that do not exist
(publish.sh / capture.sh). OP018-6434, 2026-11-15."""
from validation import (classify_environment_failure, environment_hint,
                        missing_maven_artifacts)

PARENT = ("[ERROR] Non-resolvable parent POM for com.bcbsm:mbp-coverage-service:1.0: "
          "The following artifacts could not be resolved: "
          "com.bcbsm.mem.pom:mem-starter-parent:pom:4.2.7-SNAPSHOT (absent): "
          "Could not find artifact com.bcbsm.mem.pom:mem-starter-parent:pom:4.2.7-SNAPSHOT "
          "in bcbsm-libs (file:///home/runner/work/x/maven-libs/bcbsm-libs)\n")
DEPS = ("[ERROR] Failed to execute goal on project a: Could not resolve dependencies for "
        "project x:y:jar:1: The following artifacts could not be resolved: "
        "com.bcbsm:lib-a:jar:1.2.0 (absent), com.bcbsm:lib-b:jar:tests:2.0-SNAPSHOT (absent)\n")
OLD = ("[ERROR] Failure to find com.acme:old-lib:jar:3.1 in https://repo.maven.apache.org "
       "was cached in the local repository\n")


def test_parent_pom_named_once_with_store_folder():
    assert classify_environment_failure(1, PARENT) == "dependency"
    assert missing_maven_artifacts(PARENT) == [
        ("com.bcbsm.mem.pom:mem-starter-parent:pom:4.2.7-SNAPSHOT",
         "com/bcbsm/mem/pom/mem-starter-parent/4.2.7-SNAPSHOT/")]


def test_several_dependencies_and_classifier():
    assert missing_maven_artifacts(DEPS) == [
        ("com.bcbsm:lib-a:jar:1.2.0", "com/bcbsm/lib-a/1.2.0/"),
        ("com.bcbsm:lib-b:jar:tests:2.0-SNAPSHOT", "com/bcbsm/lib-b/2.0-SNAPSHOT/")]


def test_maven_38_wording():
    assert missing_maven_artifacts(OLD) == [("com.acme:old-lib:jar:3.1", "com/acme/old-lib/3.1/")]


def test_no_artifacts_in_unrelated_output():
    assert missing_maven_artifacts("[ERROR] COMPILATION ERROR : Foo.java:[3,1]") == []
    assert missing_maven_artifacts("") == []


def test_hint_has_no_nonexistent_scripts():
    hint = environment_hint("dependency")
    assert "publish.sh" not in hint and "capture.sh" not in hint
    assert "DevOps" in hint and "Maven layout" in hint
