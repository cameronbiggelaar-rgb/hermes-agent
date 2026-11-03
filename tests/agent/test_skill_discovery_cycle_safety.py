"""Regression tests: skill discovery must stay bounded when a skill tree re-enters itself.

Skill roots are scanned on every turn (system-prompt index) and every gateway command-menu
rebuild (Telegram/Discord/Slack), and the scan follows symlinks so that legitimately symlinked
skills are discoverable. A symlinked bundle that points back into the corpus — a self-link, a
link to an ancestor, several such links (which multiply per level), the same corpus configured
twice, a nested checkout linking its parent — used to expand without bound.

These tests pin the contract: discovery terminates, visits each real directory once, keeps
finding legitimate symlinked skills, and never walks VCS internals.
"""


import os
import time
from pathlib import Path

from agent.skill_utils import get_skill_search_roots, iter_skill_index_files


def _skills_root(tmp_path: Path) -> Path:
    """An isolated skills root (tmp_path itself holds pytest fixtures too)."""
    root = tmp_path / "skills"
    root.mkdir()
    return root


def _skill(root: Path, name: str) -> Path:
    """Create ``root/name/SKILL.md`` and return the skill dir."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill\n---\n\nBody.\n", encoding="utf-8"
    )
    return d


def test_self_referential_symlink_terminates(tmp_path):
    """A skill dir containing a link to itself is walked once."""
    skills = _skills_root(tmp_path)
    alpha = _skill(skills, "alpha")
    (alpha / "self").symlink_to(".", target_is_directory=True)

    assert list(iter_skill_index_files(skills, "SKILL.md")) == [alpha / "SKILL.md"]


def test_ancestor_cycle_symlink_terminates(tmp_path):
    """A link back to the skills root must not re-enter the tree."""
    skills = _skills_root(tmp_path)
    beta = _skill(skills / "research", "beta")
    (beta / "root").symlink_to(skills, target_is_directory=True)

    assert list(iter_skill_index_files(skills, "SKILL.md")) == [beta / "SKILL.md"]


def test_bundle_linking_back_to_root_stays_bounded(tmp_path):
    """A skill-embedded bundle with several links to an ancestor (the shape a bound-skill
    bundle takes) used to multiply per level; it must add at most the bundle's own dirs."""
    from agent.skill_utils import walk_skill_dirs

    skills = _skills_root(tmp_path)
    alpha = _skill(skills, "alpha")
    bundle = alpha / ".tmp" / "bound-skills" / "research"
    bundle.mkdir(parents=True)
    for i in range(3):
        (bundle / f"back{i}").symlink_to(skills, target_is_directory=True)

    walked = [root for root, _dirs, _files in walk_skill_dirs(skills)]

    # skills, alpha, alpha/.tmp, alpha/.tmp/bound-skills, .../research — nothing re-descended.
    assert len(walked) == 5
    assert list(iter_skill_index_files(skills, "SKILL.md")) == [alpha / "SKILL.md"]


def test_mutually_linked_directories_terminate(tmp_path):
    """a -> b and b -> a: neither may be entered twice."""
    skills = _skills_root(tmp_path)
    a = _skill(skills, "a")
    b = _skill(skills, "b")
    (a / "to-b").symlink_to(b, target_is_directory=True)
    (b / "to-a").symlink_to(a, target_is_directory=True)

    assert list(iter_skill_index_files(skills, "SKILL.md")) == [a / "SKILL.md", b / "SKILL.md"]


def test_legitimate_symlinked_skill_is_still_discovered(tmp_path):
    """A skill linked in from outside the root keeps working (the reason we follow links)."""
    external = _skill(tmp_path / "external-repo", "delta")
    skills = _skills_root(tmp_path)
    (skills / "delta").symlink_to(external, target_is_directory=True)

    assert list(iter_skill_index_files(skills, "SKILL.md")) == [skills / "delta" / "SKILL.md"]


def test_two_links_to_one_skill_yield_one_entry(tmp_path):
    """Aliases of the same skill collapse to the first path, not one entry per link."""
    target = _skill(tmp_path / "corpus", "epsilon")
    skills = _skills_root(tmp_path)
    (skills / "a-alias").symlink_to(target, target_is_directory=True)
    (skills / "z-alias").symlink_to(target, target_is_directory=True)

    assert list(iter_skill_index_files(skills, "SKILL.md")) == [skills / "a-alias" / "SKILL.md"]


def test_shallow_path_wins_over_a_deep_alias(tmp_path):
    """A skill reachable both directly and through a deep bundle alias is reported once,
    under its canonical shallow path — not under the alias, which sorts first."""
    skills = _skills_root(tmp_path)
    holder = _skill(skills, "alpha")          # sorts before "zeta"
    zeta = _skill(skills, "zeta")             # the canonical, shallow path
    alias_dir = holder / ".tmp" / "bound" / "research"
    alias_dir.mkdir(parents=True)
    (alias_dir / "zeta-alias").symlink_to(zeta, target_is_directory=True)

    found = [str(p) for p in iter_skill_index_files(skills, "SKILL.md")]

    assert found == [str(holder / "SKILL.md"), str(zeta / "SKILL.md")]


def test_walk_visits_each_real_directory_once(tmp_path):
    """The scan is linear in real directories: no directory is yielded twice."""
    from agent.skill_utils import walk_skill_dirs

    skills = _skills_root(tmp_path)
    alpha = _skill(skills, "alpha")
    for name in ("dup1", "dup2", "dup3"):
        (skills / name).symlink_to(alpha, target_is_directory=True)

    walked = [root for root, _dirs, _files in walk_skill_dirs(skills)]

    assert len(walked) == len({os.path.realpath(p) for p in walked})


def test_dir_limit_caps_a_pathological_tree(tmp_path):
    """The backstop ceiling stops a tree that would otherwise keep going."""
    from agent.skill_utils import walk_skill_dirs

    skills = _skills_root(tmp_path)
    for i in range(6):
        _skill(skills, f"s{i}")

    assert len(list(walk_skill_dirs(skills, max_dirs=3))) == 3


def test_nested_git_checkout_is_ignored_and_bounded(tmp_path):
    """A vendored checkout is scanned for skills, but never through its own VCS metadata,
    and a link from it back to the parent root does not re-enter the tree."""
    skills = _skills_root(tmp_path)
    _skill(skills, "alpha")
    checkout = skills / "vendor" / "checkout"
    _skill(checkout, "vendored")
    (checkout / ".git" / "objects").mkdir(parents=True)
    (checkout / ".git" / "objects" / "SKILL.md").write_text("---\nname: ghost\n---\n", encoding="utf-8")
    (checkout / "loop").symlink_to(skills, target_is_directory=True)

    found = list(iter_skill_index_files(skills, "SKILL.md"))

    assert found == sorted([skills / "alpha" / "SKILL.md", checkout / "vendored" / "SKILL.md"])
    assert not any(".git" in p.parts for p in found)


def test_overlapping_roots_are_collapsed(tmp_path, monkeypatch):
    """A root configured twice, or nested inside a higher-precedence root, is scanned once."""
    skills = _skills_root(tmp_path)
    _skill(skills, "alpha")
    monkeypatch.setattr("agent.skill_utils.get_project_skills_dirs", lambda: [])
    monkeypatch.setattr("agent.skill_utils.get_skill_create_dir", lambda: None)
    monkeypatch.setattr(
        "agent.skill_utils.get_external_skills_dirs",
        lambda: [skills, skills / "alpha", tmp_path / "other"],
    )

    roots = [d for _tier, d in get_skill_search_roots(skills)]

    assert skills in roots
    assert skills / "alpha" not in roots  # nested inside the local root
    assert roots.count(skills) == 1


def test_shared_seen_makes_overlapping_roots_linear(tmp_path):
    """Two roots sharing one identity set do not re-walk the overlap."""
    skills = _skills_root(tmp_path)
    alpha = _skill(skills, "alpha")
    seen: set[tuple[int, int]] = set()

    first = list(iter_skill_index_files(skills, "SKILL.md", seen=seen))
    second = list(iter_skill_index_files(alpha, "SKILL.md", seen=seen))

    assert first == [alpha / "SKILL.md"]
    assert second == []


def test_skills_prompt_manifest_is_cycle_safe(tmp_path):
    """The prompt-index manifest walks the same tree and must not inflate either."""
    from agent.prompt_builder import _build_skills_manifest

    skills = _skills_root(tmp_path)
    alpha = _skill(skills, "alpha")
    (alpha / ".tmp").mkdir()
    (alpha / ".tmp" / "back").symlink_to(skills, target_is_directory=True)

    manifest = _build_skills_manifest(skills)

    assert set(manifest) == {os.path.join("alpha", "SKILL.md")}


def test_catalog_scan_signature_is_cycle_safe(tmp_path):
    """The change signature the catalog caches on must stay small on a re-entrant tree."""
    from tools.skills_tool import _skills_scan_signature

    skills = _skills_root(tmp_path)
    alpha = _skill(skills, "alpha")
    bundle = alpha / ".tmp" / "bound-skills" / "research"
    bundle.mkdir(parents=True)
    for i in range(3):
        (bundle / f"back{i}").symlink_to(skills, target_is_directory=True)

    started = time.monotonic()
    signature, _disabled, _platform = _skills_scan_signature([skills], set())

    assert time.monotonic() - started < 5
    assert len(signature) <= 10  # the five real dirs, not 3^depth


def test_telegram_command_discovery_is_bounded_on_a_cyclic_tree(tmp_path, monkeypatch):
    """The gateway menu build (the path that stalled) stays bounded end to end."""
    from hermes_cli.commands_platforms import telegram_menu_commands
    import agent.skill_commands as skill_commands
    import tools.skills_tool as skills_tool

    skills = _skills_root(tmp_path)
    alpha = _skill(skills, "alpha")
    beta = _skill(skills / "research", "beta")
    (beta / "back").symlink_to(skills, target_is_directory=True)
    # A bundle that fans out into the corpus: the shape that multiplies a plain os.walk.
    bundle = alpha / ".tmp" / "bound-skills" / "research"
    bundle.mkdir(parents=True)
    for i in range(4):
        (bundle / f"back{i}").symlink_to(skills, target_is_directory=True)

    monkeypatch.setattr(skills_tool, "SKILLS_DIR", skills)
    monkeypatch.setattr(skills_tool, "_SKILLS_CACHE", {})
    monkeypatch.setattr(skill_commands, "_skill_commands_by_key", {})
    monkeypatch.setattr("agent.skill_utils.get_project_skills_dirs", lambda: [])
    monkeypatch.setattr("agent.skill_utils.get_skill_create_dir", lambda: None)
    monkeypatch.setattr("agent.skill_utils.get_external_skills_dirs", lambda: [])
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_commands", lambda: {})

    started = time.monotonic()
    menu, hidden = telegram_menu_commands(max_commands=100)
    elapsed = time.monotonic() - started

    names = [name for name, _desc in menu]
    assert elapsed < 5, f"command discovery took {elapsed:.1f}s on a cyclic tree"
    assert "alpha" in names and "beta" in names
    assert len(names) == len(set(names))
    assert hidden == 0
