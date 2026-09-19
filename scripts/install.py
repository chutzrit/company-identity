#!/usr/bin/env python3
"""Install one shared skill into a project; preserve user-edited files."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

NAME = "company-identity"
SOURCE = Path(__file__).resolve().parents[1] / "skills" / NAME
DEFAULT_DIRS = (".claude/skills", ".agents/skills")


def fingerprint(folder):
    if not folder.is_dir() or folder.is_symlink():
        raise ValueError(f"일반 폴더가 아닙니다: {folder}")
    result = {}
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"스킬 내부 심링크를 먼저 확인하세요: {path}")
        if path.is_file():
            result[path.relative_to(folder).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def present(path):
    return os.path.lexists(path)


def child(root, relative):
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise ValueError(f"프로젝트 안의 상대 경로만 사용하세요: {relative}")
    path = root / rel
    if not path.parent.resolve().is_relative_to(root):
        raise ValueError(f"프로젝트 밖으로 연결된 상위 폴더입니다: {path.parent}")
    return path


def replace_folder(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".company-identity-", dir=destination.parent) as temp:
        staged = Path(temp) / "new"
        backup = Path(temp) / "previous"
        shutil.copytree(source, staged)
        moved = False
        try:
            if destination.exists():
                destination.rename(backup)
                moved = True
            staged.rename(destination)
        except Exception:
            if moved and not destination.exists():
                backup.rename(destination)
            raise


def install(project, agent_dirs, copy_mode):
    root = Path(project).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"대상 프로젝트 폴더가 없습니다: {root}")
    payload = fingerprint(SOURCE)
    if "SKILL.md" not in payload:
        raise ValueError("배포본의 SKILL.md가 없습니다.")
    canonical = child(root, f"skills/{NAME}")
    state_path = child(root, "skills/.company-identity-install.json")
    if state_path.is_symlink():
        raise ValueError(f"설치 기록이 심링크입니다: {state_path}")
    state = {"schema_version": 1, "copies": {}}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("schema_version") != 1 or not isinstance(state.get("copies"), dict):
            raise ValueError(f"지원하지 않는 설치 기록입니다: {state_path}")

    links = list(dict.fromkeys(str(Path(d) / NAME) for d in DEFAULT_DIRS + tuple(agent_dirs)))
    destinations = [child(root, rel) for rel in links]
    all_paths = [canonical] + destinations
    for i, a in enumerate(all_paths):
        for b in all_paths[i + 1:]:
            if a == b or a in b.parents or b in a.parents:
                raise ValueError(f"설치 경로가 겹칩니다: {a}, {b}")

    plans = []
    copies = [canonical] + (destinations if copy_mode else [])
    for target in copies:
        key = target.relative_to(root).as_posix()
        if target == SOURCE:
            plans.append(("source", target))
            continue
        if present(target):
            current = fingerprint(target)
            if current == payload:
                plans.append(("keep", target))
                continue
            if state["copies"].get(key) != current:
                raise ValueError(f"기존 파일 또는 로컬 수정이 있어 유지합니다: {target}")
        plans.append(("copy", target))

    if not copy_mode:
        for target in destinations:
            if present(target):
                if target.is_symlink() and target.resolve() == canonical.resolve():
                    plans.append(("keep-link", target))
                else:
                    raise ValueError(f"기존 연결 또는 폴더가 있어 유지합니다: {target}")
            else:
                plans.append(("link", target))

    for action, target in plans:
        if action == "copy":
            replace_folder(SOURCE, target)
        elif action == "link":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(os.path.relpath(canonical, target.parent), target_is_directory=True)
        print(f"{action}: {target}")

    for target in copies:
        state["copies"][target.relative_to(root).as_posix()] = payload
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=state_path.parent, delete=False) as temp:
        json.dump(state, temp, ensure_ascii=False, indent=2)
        temp.write("\n")
        staged_state = Path(temp.name)
    staged_state.replace(state_path)

    for target in all_paths:
        resolved = target.resolve()
        if fingerprint(resolved) != payload:
            raise ValueError(f"설치 후 내용 확인에 실패했습니다: {target}")
    print("파일과 참조 문서 확인 완료. 새 에이전트 세션에서 스킬 인식을 확인하세요.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="대상 프로젝트 폴더")
    parser.add_argument("--agent-dir", action="append", default=[], help="추가 에이전트의 프로젝트 스킬 상대 경로")
    parser.add_argument("--copy", action="store_true", help="심링크 대신 각 경로에 복사")
    args = parser.parse_args()
    try:
        install(args.project, args.agent_dir, args.copy)
    except (OSError, ValueError, TypeError) as exc:
        print(f"설치 중단: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
