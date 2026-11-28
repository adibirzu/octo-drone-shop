"""Static contracts for image identity propagation."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_requires_and_exports_source_commit():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert re.search(r"(?m)^ARG SOURCE_COMMIT\s*$", dockerfile)
    assert 'GIT_SHA=${SOURCE_COMMIT}' in dockerfile
    assert '^[0-9a-f]{40}$' in dockerfile


def test_all_deploy_build_commands_pass_validated_source_commit():
    builds = []
    for path in (ROOT / "deploy").rglob("*"):
        if path.is_file() and path.suffix in {".sh", ".yaml", ".yml"}:
            for line in path.read_text().splitlines():
                if re.search(r"\bdocker\s+(?:buildx\s+)?build\s", line) and not line.lstrip().startswith("#"):
                    builds.append((path, line))
    assert builds
    for path, line in builds:
        assert '--build-arg SOURCE_COMMIT=${SOURCE_COMMIT}' in line, str(path)
        assert '^[0-9a-f]{40}$' in path.read_text(), str(path)


def test_manifests_do_not_override_image_identity():
    for name in ("deploy/k8s/deployment.yaml", "deploy/vm/docker-compose-unified.yml"):
        assert not re.search(r"(?m)^\s*(?:- name: GIT_SHA|GIT_SHA:)", (ROOT / name).read_text())
