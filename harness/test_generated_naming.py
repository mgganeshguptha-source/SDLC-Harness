"""Coding/planning prompts name the classes openapi-generator will produce."""
from sdk_runner import _generated_naming_block

POM = """<project><build><plugins><plugin>
<artifactId>openapi-generator-maven-plugin</artifactId>
<configuration>
  <inputSpec>${project.basedir}/src/main/resources/openapi.yml</inputSpec>
  <modelNameSuffix>Dto</modelNameSuffix>
  <modelPackage>org.x.rest.dto</modelPackage>
  <apiPackage>org.x.rest.api</apiPackage>
</configuration></plugin></plugins></build></project>"""


def test_naming_block_from_pom(tmp_path):
    (tmp_path / "pom.xml").write_text(POM)
    block = _generated_naming_block(tmp_path)
    assert "src/main/resources/openapi.yml" in block
    assert "<SchemaNameDto> in package org.x.rest.dto" in block
    assert "API interfaces in org.x.rest.api" in block
    assert "class `OwnerDto`" in block


def test_no_block_without_generator(tmp_path):
    (tmp_path / "pom.xml").write_text("<project><artifactId>app</artifactId></project>")
    assert _generated_naming_block(tmp_path) == ""
