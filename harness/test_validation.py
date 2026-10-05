"""
test_validation.py - what a failed build reports back to the fixing phase.

Run:  python -m pytest harness/test_validation.py -v
(or:  python harness/test_validation.py   for a no-pytest fallback)
"""
from validation import maven_error_summary

# Shape of `mvn -e test` failing on a formatting check: the reason comes first,
# then a long stack trace, then boilerplate.
_FORMAT_FAILURE = "\n".join(
    ["[INFO] BUILD FAILURE",
     "[ERROR] Failed to execute goal io.spring.javaformat:spring-javaformat-maven-plugin:"
     "0.0.47:validate (default) on project demo: Formatting violations found in the "
     "following files:",
     "[ERROR]  * /work/service/src/main/java/demo/Vet.java",
     "[ERROR] ",
     "[ERROR] Run `spring-javaformat:apply` to fix.",
     "[ERROR] -> [Help 1]",
     "org.apache.maven.lifecycle.LifecycleExecutionException: Failed"]
    + ["    at org.apache.maven.Foo.bar (Foo.java:%d)" % i for i in range(40)]
    + ["[ERROR] ",
       "[ERROR] Re-run Maven using the -X switch to enable full debug logging.",
       "[ERROR] For more information about the errors and possible solutions, please read the following articles:",
       "[ERROR] [Help 1] http://cwiki.apache.org/confluence/display/MAVEN/MojoFailureException"])


def test_keeps_the_reason_and_the_file():
    s = maven_error_summary(_FORMAT_FAILURE)
    assert "Formatting violations found" in s
    assert "src/main/java/demo/Vet.java" in s
    assert "spring-javaformat:apply" in s


def test_drops_boilerplate_and_stack_trace():
    s = maven_error_summary(_FORMAT_FAILURE)
    assert "Re-run Maven" not in s
    assert "[Help 1]" not in s
    assert "Foo.java" not in s


def test_compile_error_lines_kept():
    out = ("[ERROR] COMPILATION ERROR : \n"
           "[ERROR] /work/src/main/java/demo/Vet.java:[42,5] cannot find symbol\n"
           "[ERROR] -> [Help 1]\n")
    s = maven_error_summary(out)
    assert "Vet.java:[42,5] cannot find symbol" in s


def test_success_output_gives_nothing():
    assert maven_error_summary("[INFO] BUILD SUCCESS\n") == ""


# ---- no-pytest fallback runner ------------------------------------------
if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {fn.__name__}  {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
