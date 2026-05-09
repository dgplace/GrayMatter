"""
@file dependencies.py
@brief Dependency-resolution and cycle-materialization helpers for ingestion.
"""

import hashlib
import json
import posixpath
import re
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path
from typing import Optional

try:
    import tomllib
except ImportError:
    import tomli as tomllib

def _candidate_internal_import_paths(
    source_rel_path: str,
    module: str,
    language: Optional[str],
) -> list[str]:
    """@brief Build repository-relative candidate file paths for an import.

    @param source_rel_path Source file path relative to the repository root.
    @param module Imported module token from the parser.
    @param language Language label for import semantics.
    @return Ordered candidate file paths that could back the import.
    """
    if not module:
        return []

    if language in {"typescript", "javascript", "tsx", "jsx"}:
        if not module.startswith("."):
            return []
        source_dir = posixpath.dirname(source_rel_path)
        base = posixpath.normpath(posixpath.join(source_dir, module))
        candidates = [base]
        if not posixpath.splitext(base)[1]:
            for extension in (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts"):
                candidates.append(f"{base}{extension}")
            for extension in (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts"):
                candidates.append(posixpath.join(base, f"index{extension}"))
        return [candidate for candidate in candidates if candidate and not candidate.startswith("../")]

    if language == "java":
        base = module.replace(".", "/")
        if not base:
            return []
        return [f"{base}.java"]

    if language in {"c", "cpp"}:
        source_dir = posixpath.dirname(source_rel_path)
        if module.startswith("/"):
            normalized = posixpath.normpath(module.lstrip("/"))
            return [normalized] if normalized and not normalized.startswith("../") else []
        candidates = [
            posixpath.normpath(posixpath.join(source_dir, module)),
            posixpath.normpath(module),
        ]
        return [candidate for candidate in candidates if candidate and not candidate.startswith("../")]

    if language in {"csharp", "swift"}:
        base = module.replace(".", "/")
        candidates = [base] if base else []
        if base and not posixpath.splitext(base)[1]:
            if language == "csharp":
                candidates.append(f"{base}.cs")
            else:
                candidates.append(f"{base}.swift")
                candidates.append(posixpath.join("Sources", base, f"{module}.swift"))
        return [candidate for candidate in candidates if candidate and not candidate.startswith("../")]

    if language != "python":
        return []

    module_dots = 0
    while module.startswith("."):
        module_dots += 1
        module = module[1:]

    if module_dots > 0:
        base_dir = posixpath.dirname(source_rel_path)
        for _ in range(max(module_dots - 1, 0)):
            base_dir = posixpath.dirname(base_dir)
        module_path = module.replace(".", "/")
        base = posixpath.normpath(posixpath.join(base_dir, module_path)) if module_path else base_dir
    else:
        module_path = module.replace(".", "/")
        if not module_path:
            return []
        base = posixpath.normpath(module_path)

    if not base or base.startswith("../"):
        return []
    return [f"{base}.py", posixpath.join(base, "__init__.py")]


def _resolve_internal_import_target_file_id(
    cur,
    repo_name: str,
    source_rel_path: str,
    module: str,
    language: Optional[str],
) -> Optional[int]:
    """@brief Resolve a dependency module token to an internal target file id.

    @param cur Open database cursor.
    @param repo_name Repository identifier.
    @param source_rel_path Source file path relative to repo root.
    @param module Imported module token.
    @param language Source file language.
    @return Internal target file id when found, otherwise None.
    """
    for candidate in _candidate_internal_import_paths(source_rel_path, module, language):
        cur.execute(
            """
            SELECT id
            FROM files
            WHERE repo = %s
              AND (
                  path = %s
                  OR path LIKE %s
              )
            LIMIT 1
            """,
            (
                repo_name,
                candidate,
                f"{candidate}.%",
            ),
        )
        row = cur.fetchone()
        if row:
            return int(row[0])
    return None


def _resolve_imported_symbol_id(
    cur,
    target_file_id: Optional[int],
    imported_name: Optional[str],
) -> Optional[int]:
    """@brief Resolve an imported exported symbol inside a target file.

    @param cur Open database cursor.
    @param target_file_id Internal target file id for the import module.
    @param imported_name Imported exported symbol name.
    @return Symbol id when the imported symbol resolves, otherwise None.
    """
    if target_file_id is None or not imported_name or imported_name in {"*", "default"}:
        return None
    cur.execute(
        """
        SELECT id
        FROM symbols
        WHERE file_id = %s
          AND lower(name) = lower(%s)
          AND is_exported = TRUE
        ORDER BY
            CASE WHEN is_primary_declaration THEN 0 ELSE 1 END,
            CASE WHEN declared_in_extension THEN 1 ELSE 0 END,
            start_line
        LIMIT 1
        """,
        (target_file_id, imported_name),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


@lru_cache(maxsize=32)
def _manifest_versions(repo_root_str: str) -> dict[str, dict[str, str]]:
    """@brief Build per-ecosystem package-version maps from repository manifests.

    @param repo_root_str Absolute repository root path string.
    @return Mapping keyed by ecosystem name (`npm`, `pip`, `maven`).
    """
    repo_root = Path(repo_root_str)
    return {
        "npm": _npm_manifest_versions(repo_root),
        "pip": _pip_manifest_versions(repo_root),
        "maven": _maven_manifest_versions(repo_root),
    }


def _npm_manifest_versions(repo_root: Path) -> dict[str, str]:
    """@brief Parse npm dependency versions from package.json files.

    @param repo_root Repository root path.
    @return Mapping of npm package name to declared version specifier.
    """
    versions: dict[str, str] = {}
    for package_json in repo_root.rglob("package.json"):
        if "node_modules" in package_json.parts:
            continue
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except Exception:
            continue
        for section in (
            "dependencies",
            "devDependencies",
            "peerDependencies",
            "optionalDependencies",
        ):
            deps = data.get(section, {})
            if isinstance(deps, dict):
                for name, version in deps.items():
                    if isinstance(name, str) and isinstance(version, str) and name not in versions:
                        versions[name] = version
    return versions


def _pip_manifest_versions(repo_root: Path) -> dict[str, str]:
    """@brief Parse Python package versions from requirements and pyproject files.

    @param repo_root Repository root path.
    @return Mapping of package name to version or constraint specifier.
    """
    versions: dict[str, str] = {}
    requirement_pattern = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*([<>=!~]{1,2}\s*[^;#\s]+)?")

    for requirements_file in repo_root.rglob("requirements.txt"):
        if ".venv" in requirements_file.parts:
            continue
        try:
            for line in requirements_file.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or stripped.startswith("-"):
                    continue
                if "@" in stripped and "://" in stripped:
                    continue
                match = requirement_pattern.match(stripped)
                if not match:
                    continue
                package = match.group(1).lower().replace("_", "-")
                raw_version = (match.group(2) or "").replace(" ", "")
                if package and package not in versions:
                    versions[package] = raw_version or "unversioned"
        except Exception:
            continue

    for pyproject_file in repo_root.rglob("pyproject.toml"):
        try:
            with pyproject_file.open("rb") as handle:
                data = tomllib.load(handle)
        except Exception:
            continue

        project_deps = data.get("project", {}).get("dependencies", [])
        if isinstance(project_deps, list):
            for raw_dep in project_deps:
                if not isinstance(raw_dep, str):
                    continue
                match = requirement_pattern.match(raw_dep)
                if not match:
                    continue
                package = match.group(1).lower().replace("_", "-")
                raw_version = (match.group(2) or "").replace(" ", "")
                if package and package not in versions:
                    versions[package] = raw_version or "unversioned"

        poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
        if isinstance(poetry_deps, dict):
            for package, raw_version in poetry_deps.items():
                if not isinstance(package, str) or package.lower() == "python":
                    continue
                normalized_package = package.lower().replace("_", "-")
                if isinstance(raw_version, str):
                    versions.setdefault(normalized_package, raw_version or "unversioned")
                elif isinstance(raw_version, dict):
                    version_value = raw_version.get("version")
                    if isinstance(version_value, str):
                        versions.setdefault(normalized_package, version_value or "unversioned")

    return versions


def _maven_manifest_versions(repo_root: Path) -> dict[str, str]:
    """@brief Parse Maven dependency versions from pom.xml files.

    @param repo_root Repository root path.
    @return Mapping of group-id and group:artifact keys to version strings.
    """
    versions: dict[str, str] = {}
    for pom_file in repo_root.rglob("pom.xml"):
        try:
            root = ET.parse(pom_file).getroot()
        except Exception:
            continue

        namespace_match = re.match(r"^\{(.+)\}", root.tag)
        namespace = {"m": namespace_match.group(1)} if namespace_match else {}
        dependency_query = ".//m:dependencies/m:dependency" if namespace else ".//dependencies/dependency"
        group_query = "m:groupId" if namespace else "groupId"
        artifact_query = "m:artifactId" if namespace else "artifactId"
        version_query = "m:version" if namespace else "version"

        for dep in root.findall(dependency_query, namespace):
            group_node = dep.find(group_query, namespace)
            artifact_node = dep.find(artifact_query, namespace)
            version_node = dep.find(version_query, namespace)
            if group_node is None or artifact_node is None or version_node is None:
                continue
            group_id = (group_node.text or "").strip()
            artifact_id = (artifact_node.text or "").strip()
            version = (version_node.text or "").strip()
            if not group_id or not artifact_id or not version:
                continue
            versions.setdefault(group_id, version)
            versions.setdefault(f"{group_id}:{artifact_id}", version)
    return versions


def _external_package_from_module(module: str, language: Optional[str]) -> str:
    """@brief Normalize an imported module token to an external package name.

    @param module Parsed module token from dependency extraction.
    @param language Source file language.
    @return External package identifier used for storage and version lookup.
    """
    if language in {"typescript", "javascript", "tsx", "jsx"}:
        if module.startswith("@"):
            parts = module.split("/")
            return "/".join(parts[:2]) if len(parts) >= 2 else module
        return module.split("/", 1)[0]

    if language == "python":
        return module.split(".", 1)[0].replace("_", "-")

    if language == "java":
        parts = module.split(".")
        if len(parts) >= 3:
            return ".".join(parts[:3])
        return module

    if language in {"c", "cpp"}:
        token = module.strip("<>\"")
        return token.split("/", 1)[0]

    if language in {"csharp", "swift"}:
        return module

    return module


def _external_version_for_package(
    package_name: str,
    module: str,
    language: Optional[str],
    manifest_versions: dict[str, dict[str, str]],
) -> Optional[str]:
    """@brief Resolve external dependency version from manifest maps.

    @param package_name Normalized external package name.
    @param module Full module token.
    @param language Source language.
    @param manifest_versions Cached ecosystem version maps.
    @return Version string when manifest data exists, otherwise None.
    """
    if language in {"typescript", "javascript", "tsx", "jsx"}:
        return manifest_versions.get("npm", {}).get(package_name)

    if language == "python":
        return manifest_versions.get("pip", {}).get(package_name.lower().replace("_", "-"))

    if language == "java":
        maven_versions = manifest_versions.get("maven", {})
        if package_name in maven_versions:
            return maven_versions[package_name]
        prefix_matches = [
            (key, value)
            for key, value in maven_versions.items()
            if ":" not in key and (module == key or module.startswith(f"{key}."))
        ]
        if prefix_matches:
            prefix_matches.sort(key=lambda row: len(row[0]), reverse=True)
            return prefix_matches[0][1]
    return None


def _tarjan_strongly_connected_components(adjacency: dict[int, set[int]]) -> list[list[int]]:
    """@brief Compute strongly connected components for a directed graph.

    @param adjacency Directed graph adjacency keyed by node id.
    @return List of strongly connected components as node-id lists.
    """
    index = 0
    index_map: dict[int, int] = {}
    low_link_map: dict[int, int] = {}
    stack: list[int] = []
    on_stack: set[int] = set()
    components: list[list[int]] = []

    def strong_connect(node: int) -> None:
        nonlocal index
        index_map[node] = index
        low_link_map[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for neighbor in adjacency.get(node, set()):
            if neighbor not in index_map:
                strong_connect(neighbor)
                low_link_map[node] = min(low_link_map[node], low_link_map[neighbor])
            elif neighbor in on_stack:
                low_link_map[node] = min(low_link_map[node], index_map[neighbor])

        if low_link_map[node] == index_map[node]:
            component: list[int] = []
            while stack:
                current = stack.pop()
                on_stack.remove(current)
                component.append(current)
                if current == node:
                    break
            components.append(component)

    for node in adjacency:
        if node not in index_map:
            strong_connect(node)
    return components


def materialize_dependency_cycles(conn, repo_name: str) -> int:
    """@brief Rebuild dependency cycle rows for a repository using SCC detection.

    @param conn Open database connection.
    @param repo_name Repository identifier.
    @return Number of cycle rows written for the repository.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT d.source_file_id, sf.path, d.target_file_id, tf.path
        FROM dependencies d
        JOIN files sf ON sf.id = d.source_file_id
        JOIN files tf ON tf.id = d.target_file_id
        WHERE sf.repo = %s
          AND tf.repo = %s
          AND d.target_file_id IS NOT NULL
        """,
        (repo_name, repo_name),
    )
    rows = cur.fetchall()

    adjacency: dict[int, set[int]] = {}
    file_paths: dict[int, str] = {}
    for source_file_id, source_path, target_file_id, target_path in rows:
        source_id = int(source_file_id)
        target_id = int(target_file_id)
        adjacency.setdefault(source_id, set()).add(target_id)
        adjacency.setdefault(target_id, set())
        file_paths[source_id] = source_path
        file_paths[target_id] = target_path

    components = _tarjan_strongly_connected_components(adjacency)
    cycle_rows: list[tuple[str, list[int], list[str], int]] = []
    for component in components:
        component_ids = sorted(component)
        if len(component_ids) == 1:
            node = component_ids[0]
            if node not in adjacency.get(node, set()):
                continue
        member_pairs = sorted(
            ((member_id, file_paths.get(member_id, str(member_id))) for member_id in component_ids),
            key=lambda pair: pair[1],
        )
        member_file_ids = [member_id for member_id, _ in member_pairs]
        member_paths = [path for _, path in member_pairs]
        cycle_hash = hashlib.sha256("\n".join(member_paths).encode("utf-8")).hexdigest()
        cycle_rows.append((cycle_hash, member_file_ids, member_paths, len(member_file_ids)))

    cur.execute("DELETE FROM dependency_cycles WHERE repo = %s", (repo_name,))
    for cycle_hash, member_file_ids, member_paths, cycle_size in cycle_rows:
        cur.execute(
            """
            INSERT INTO dependency_cycles (repo, cycle_hash, member_file_ids, member_paths, cycle_size)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (repo, cycle_hash) DO UPDATE
            SET member_file_ids = EXCLUDED.member_file_ids,
                member_paths = EXCLUDED.member_paths,
                cycle_size = EXCLUDED.cycle_size,
                created_at = NOW()
            """,
            (repo_name, cycle_hash, member_file_ids, member_paths, cycle_size),
        )
    conn.commit()
    return len(cycle_rows)


