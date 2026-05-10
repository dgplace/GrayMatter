"""
@file framework_diagnostics.py
@brief Callback-framework registry detection and missing-extractor diagnostics persistence.
"""

import json
from typing import Any

CALLBACK_FRAMEWORK_REGISTRY: tuple[dict[str, Any], ...] = (
    {
        "framework": "Express",
        "extractor_module": None,
        "dependency_modules": ("express",),
        "dependency_prefixes": (),
        "reference_targets": (),
    },
    {
        "framework": "FastAPI",
        "extractor_module": None,
        "dependency_modules": ("fastapi",),
        "dependency_prefixes": (),
        "reference_targets": (),
    },
    {
        "framework": "Flask",
        "extractor_module": None,
        "dependency_modules": ("flask",),
        "dependency_prefixes": (),
        "reference_targets": (),
    },
    {
        "framework": "React (useEffect)",
        "extractor_module": None,
        "dependency_modules": ("react",),
        "dependency_prefixes": (),
        "reference_targets": ("useeffect",),
    },
    {
        "framework": "Node EventEmitter",
        "extractor_module": None,
        "dependency_modules": ("events",),
        "dependency_prefixes": (),
        "reference_targets": ("eventemitter",),
    },
    {
        "framework": "DOM addEventListener",
        "extractor_module": None,
        "dependency_modules": (),
        "dependency_prefixes": (),
        "reference_targets": ("addeventlistener",),
    },
    {
        "framework": "NestJS",
        "extractor_module": None,
        "dependency_modules": ("@nestjs/common", "@nestjs/core"),
        "dependency_prefixes": ("@nestjs/",),
        "reference_targets": (),
    },
    {
        "framework": "Spring",
        "extractor_module": None,
        "dependency_modules": (),
        "dependency_prefixes": ("org.springframework",),
        "reference_targets": ("restcontroller", "requestmapping"),
    },
    {
        "framework": "Qt signals/slots",
        "extractor_module": None,
        "dependency_modules": (),
        "dependency_prefixes": ("qt",),
        "reference_targets": ("signals", "slots", "connect"),
    },
)


def _matches_dependency(entry: dict[str, Any], external_module: str, imported_name: str) -> bool:
    """@brief Return whether dependency metadata matches a framework signature."""
    if not external_module and not imported_name:
        return False

    dependency_modules = entry.get("dependency_modules", ())
    dependency_prefixes = entry.get("dependency_prefixes", ())

    if external_module in dependency_modules or imported_name in dependency_modules:
        return True
    if any(external_module.startswith(prefix) for prefix in dependency_prefixes):
        return True
    if any(imported_name.startswith(prefix) for prefix in dependency_prefixes):
        return True
    return False


def _matches_reference(entry: dict[str, Any], target_name: str) -> bool:
    """@brief Return whether a reference target name matches a framework signature."""
    if not target_name:
        return False
    return target_name in entry.get("reference_targets", ())


def detect_callback_frameworks(cur, repo_name: str) -> list[dict[str, Any]]:
    """@brief Detect callback-binding frameworks used in a repository.

    @param cur Open database cursor.
    @param repo_name Repository name.
    @return Detected frameworks with unique affected-file counts and ids.
    """
    file_ids_by_framework: dict[str, set[int]] = {
        entry["framework"]: set() for entry in CALLBACK_FRAMEWORK_REGISTRY
    }

    cur.execute(
        """
        SELECT d.source_file_id, lower(COALESCE(d.external_module, '')), lower(COALESCE(d.imported_name, ''))
        FROM dependencies d
        JOIN files f ON f.id = d.source_file_id
        WHERE f.repo = %s
        """,
        (repo_name,),
    )
    for source_file_id, external_module, imported_name in cur.fetchall():
        for entry in CALLBACK_FRAMEWORK_REGISTRY:
            if _matches_dependency(entry, external_module, imported_name):
                file_ids_by_framework[entry["framework"]].add(int(source_file_id))

    cur.execute(
        """
        SELECT sr.source_file_id, lower(COALESCE(sr.target_name, ''))
        FROM symbol_references sr
        JOIN files f ON f.id = sr.source_file_id
        WHERE f.repo = %s
        """,
        (repo_name,),
    )
    for source_file_id, target_name in cur.fetchall():
        for entry in CALLBACK_FRAMEWORK_REGISTRY:
            if _matches_reference(entry, target_name):
                file_ids_by_framework[entry["framework"]].add(int(source_file_id))

    detected: list[dict[str, Any]] = []
    for entry in CALLBACK_FRAMEWORK_REGISTRY:
        framework = entry["framework"]
        affected_file_ids = sorted(file_ids_by_framework[framework])
        if not affected_file_ids:
            continue
        detected.append(
            {
                "framework": framework,
                "extractor_module": entry.get("extractor_module"),
                "affected_file_count": len(affected_file_ids),
                "affected_file_ids": affected_file_ids,
            }
        )
    detected.sort(key=lambda item: (-item["affected_file_count"], item["framework"]))
    return detected


def materialize_missing_extractor_diagnostics(conn, repo_name: str) -> int:
    """@brief Persist missing-extractor diagnostics for a repository.

    @param conn Open database connection.
    @param repo_name Repository name.
    @return Number of missing-extractor diagnostics persisted.
    """
    cur = conn.cursor()
    try:
        detected = detect_callback_frameworks(cur, repo_name)
        cur.execute(
            """
            DELETE FROM ingestion_diagnostics
            WHERE repo = %s
              AND diagnostic_kind = 'missing_extractor'
            """,
            (repo_name,),
        )

        inserted = 0
        for framework in detected:
            if framework["extractor_module"] is not None:
                continue
            details_json = json.dumps(
                {
                    "framework": framework["framework"],
                    "affected_file_count": framework["affected_file_count"],
                }
            )
            cur.execute(
                """
                INSERT INTO ingestion_diagnostics (
                    repo,
                    diagnostic_kind,
                    framework,
                    extractor_module,
                    affected_file_count,
                    affected_file_ids,
                    details,
                    updated_at
                )
                VALUES (%s, 'missing_extractor', %s, %s, %s, %s, %s::jsonb, NOW())
                ON CONFLICT (repo, diagnostic_kind, framework)
                DO UPDATE
                SET extractor_module = EXCLUDED.extractor_module,
                    affected_file_count = EXCLUDED.affected_file_count,
                    affected_file_ids = EXCLUDED.affected_file_ids,
                    details = EXCLUDED.details,
                    updated_at = NOW()
                """,
                (
                    repo_name,
                    framework["framework"],
                    framework["extractor_module"],
                    framework["affected_file_count"],
                    framework["affected_file_ids"],
                    details_json,
                ),
            )
            inserted += 1

        conn.commit()
        return inserted
    finally:
        cur.close()
