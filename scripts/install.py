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
CANONICAL = f".agents/skills/{NAME}"
STATE = ".agents/skills/.company-identity-install.json"
DEFAULT_DIRS = (".claude/skills",)
# Earlier versions kept the original under skills/ and linked both agent paths to it.
LEGACY = f"skills/{NAME}"
LEGACY_STATE = "skills/.company-identity-install.json"


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


def load_state(state_path):
    if state_path.is_symlink():
        raise ValueError(f"설치 기록이 심링크입니다: {state_path}")
    if not state_path.exists():
        return {"schema_version": 1, "copies": {}}
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("schema_version") != 1 or not isinstance(state.get("copies"), dict):
        raise ValueError(f"지원하지 않는 설치 기록입니다: {state_path}")
    return state


def write_state(state_path, state):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=state_path.parent, delete=False) as temp:
        json.dump(state, temp, ensure_ascii=False, indent=2)
        temp.write("\n")
        staged_state = Path(temp.name)
    staged_state.replace(state_path)


def migrate_legacy(root, canonical, state_path, destinations):
    """Move a skills/ original left by earlier versions to the canonical path; local edits move with it."""
    legacy = child(root, LEGACY)
    legacy_state = child(root, LEGACY_STATE)
    if legacy.is_symlink() or not (legacy / "SKILL.md").is_file():
        return False
    target = legacy.resolve()
    stale = [p for p in [canonical] + destinations if p.is_symlink() and p.resolve() == target]
    if not stale and not legacy_state.is_file():
        print(f"참고: 이 도구가 설치한 흔적이 없어 그대로 둡니다: {legacy}")
        return False

    old = load_state(legacy_state)
    if present(state_path):
        raise ValueError(f"예전 설치 기록과 새 설치 기록이 함께 있습니다. 하나만 남겨 주세요: {legacy_state}, {state_path}")
    current = fingerprint(legacy)
    if present(canonical) and canonical not in stale:
        if canonical.is_symlink():
            raise ValueError(f"기존 연결 또는 폴더가 있어 유지합니다: {canonical}")
        if current not in (fingerprint(canonical), old["copies"].get(LEGACY)):
            raise ValueError(f"스킬 원본이 두 곳에 있고 내용이 다릅니다. 남길 쪽을 확인해 주세요: {legacy}, {canonical}")
        move = False
    else:
        move = True

    for link in stale:
        link.unlink()
    if move:
        canonical.parent.mkdir(parents=True, exist_ok=True)
        legacy.rename(canonical)
        print(f"migrate: {legacy} → {canonical}")
    else:
        shutil.rmtree(legacy)
        print(f"migrate: 수정 없는 중복 원본 제거 {legacy}")
    # Relink right away so the agent paths keep working even if the content update is refused below.
    for link in stale:
        if link != canonical:
            link.symlink_to(os.path.relpath(canonical, link.parent), target_is_directory=True)
            print(f"migrate: 연결 갱신 {link}")

    previous = old["copies"].pop(LEGACY, None)
    if move and previous is not None:
        old["copies"][CANONICAL] = previous
    write_state(state_path, old)
    if legacy_state.is_file():
        legacy_state.unlink()
    if not any(legacy.parent.iterdir()):
        legacy.parent.rmdir()
    return True


def install(project, agent_dirs, copy_mode):
    root = Path(project).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"대상 프로젝트 폴더가 없습니다: {root}")
    payload = fingerprint(SOURCE)
    if "SKILL.md" not in payload:
        raise ValueError("배포본의 SKILL.md가 없습니다.")
    # Inside the distribution repo the source itself stays the original; agent paths link to it.
    in_source_repo = child(root, LEGACY).resolve() == SOURCE
    canonical = SOURCE if in_source_repo else child(root, CANONICAL)
    state_path = child(root, LEGACY_STATE if in_source_repo else STATE)
    link_dirs = DEFAULT_DIRS + ((".agents/skills",) if in_source_repo else ()) + tuple(agent_dirs)

    links = list(dict.fromkeys(str(Path(d) / NAME) for d in link_dirs))
    destinations = [child(root, rel) for rel in links]
    destinations = [d for d in destinations if d != canonical]
    all_paths = [canonical] + destinations
    for i, a in enumerate(all_paths):
        for b in all_paths[i + 1:]:
            if a == b or a in b.parents or b in a.parents:
                raise ValueError(f"설치 경로가 겹칩니다: {a}, {b}")

    migrated = False if in_source_repo else migrate_legacy(root, canonical, state_path, destinations)
    state = load_state(state_path)

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
                baseline = state["copies"].get(key) or payload
                changed = sorted(f for f in current.keys() | baseline.keys() if current.get(f) != baseline.get(f))
                note = " 구조는 새 위치로 옮겼고 내용은 갱신하지 않았습니다." if migrated else ""
                raise ValueError(f"기존 파일 또는 로컬 수정이 있어 유지합니다: {target} (달라진 파일: {', '.join(changed)}){note}")
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
    write_state(state_path, state)

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
