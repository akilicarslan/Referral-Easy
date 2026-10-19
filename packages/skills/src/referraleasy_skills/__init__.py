"""Versioned skill packages and their loader (ADR-0028).

Typical use, in the two phases the format exists to separate::

    from referraleasy_skills import list_manifests, load_skill

    # selection: manifest fields only
    for manifest in list_manifests(owner_agent="scheduling"):
        render(manifest.summary, manifest.when_to_use)

    # use: instructions and references, for packages already selected
    skill = load_skill("booking-confirmation")

Nothing in this package calls a model, opens a network socket, or reads a credential, and no
manifest in it can grant a tool permission.
"""

from referraleasy_skills.loader import (
    AGENT_ROLES,
    AUTHORITY_INSTRUCTIONS_ONLY,
    FORBIDDEN_MANIFEST_KEYS,
    INSTRUCTIONS_FILENAME,
    MANIFEST_FILENAME,
    REFERENCES_DIRNAME,
    REQUIRED_MANIFEST_KEYS,
    REQUIRED_SECTIONS,
    Skill,
    SkillError,
    SkillManifest,
    SkillManifestError,
    SkillNotFoundError,
    list_manifests,
    list_skills,
    load_manifest,
    load_skill,
    manifest_schema,
    select,
    skills_root,
)

__all__ = [
    "AGENT_ROLES",
    "AUTHORITY_INSTRUCTIONS_ONLY",
    "FORBIDDEN_MANIFEST_KEYS",
    "INSTRUCTIONS_FILENAME",
    "MANIFEST_FILENAME",
    "REFERENCES_DIRNAME",
    "REQUIRED_MANIFEST_KEYS",
    "REQUIRED_SECTIONS",
    "Skill",
    "SkillError",
    "SkillManifest",
    "SkillManifestError",
    "SkillNotFoundError",
    "list_manifests",
    "list_skills",
    "load_manifest",
    "load_skill",
    "manifest_schema",
    "select",
    "skills_root",
]
