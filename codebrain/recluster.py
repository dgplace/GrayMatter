"""
@file recluster.py
@brief Rebuild repository clusters/module-intents without re-indexing file content.

This command reruns only the post-ingestion structural phases:
1) cluster materialization (`clusters` + `cluster_members`)
2) optional logical-module synthesis (`module_intents.kind='logical'`)

It does not parse, chunk, or embed source files again.
"""

import click
from rich.console import Console

from codebrain.classifier import IntentClassifier
from codebrain.embedder import EmbeddingClient
from codebrain.ingest import get_db, load_config
from codebrain.ingestion.clusters import materialize_clusters
from codebrain.synthesize_modules import synthesize_logical_modules

console = Console()


class _NoClassifyIntentClassifier:
    """@brief Minimal classifier stub used when synthesis should avoid LLM calls."""

    def _generate(self, prompt: str, max_tokens: int = 0) -> str:
        """@brief Return no model output so synthesis code falls back deterministically."""
        raise RuntimeError("classification disabled")

    def _parse_json(self, payload: str) -> dict:
        """@brief Return empty parse payload for compatibility with synthesis call sites."""
        return {}


def _resolve_target_resolution(cfg: dict, resolution: float | None, multiplier: float) -> tuple[float, float]:
    """@brief Resolve base and effective clustering resolution for this run.

    @param cfg Loaded configuration dictionary.
    @param resolution Optional explicit absolute resolution override.
    @param multiplier Multiplier applied to configured/base resolution when no explicit override is set.
    @return Tuple of `(base_resolution, target_resolution)`.
    """
    clustering_cfg = cfg.get("clustering", {})
    base_resolution = float(clustering_cfg.get("resolution", 1.0) or 1.0)
    if resolution is not None:
        return base_resolution, float(resolution)
    return base_resolution, base_resolution * float(multiplier)


def _assert_repo_exists(conn, repo_name: str) -> None:
    """@brief Fail fast when a repository has not been indexed yet.

    @param conn Open database connection.
    @param repo_name Repository identifier persisted in `files.repo`.
    @raises click.ClickException When no rows exist for the repo.
    """
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM files WHERE repo = %s LIMIT 1", (repo_name,))
    if cur.fetchone() is None:
        raise click.ClickException(
            f"Repository `{repo_name}` is not indexed. Run ingestion first."
        )


@click.command()
@click.option("--repo-name", required=True, help="Indexed repository identifier (files.repo).")
@click.option("--config", default="codebrain.toml", show_default=True, help="Config file path.")
@click.option("--resolution", type=float, default=None, help="Absolute clustering resolution override.")
@click.option(
    "--resolution-multiplier",
    type=float,
    default=2.0,
    show_default=True,
    help="Multiplier applied to configured clustering resolution when --resolution is omitted.",
)
@click.option("--min-files", default=3, show_default=True, help="Minimum files per logical module.")
@click.option(
    "--synthesize-logical/--no-synthesize-logical",
    default=True,
    show_default=True,
    help="Refresh logical module_intents from rebuilt clusters.",
)
@click.option(
    "--no-classify",
    is_flag=True,
    default=False,
    help="Skip LLM classification for cluster/module naming (deterministic fallback metadata).",
)
def main(
    repo_name: str,
    config: str,
    resolution: float | None,
    resolution_multiplier: float,
    min_files: int,
    synthesize_logical: bool,
    no_classify: bool,
) -> None:
    """@brief Rebuild clusters (and optionally logical modules) for an indexed repository."""
    if resolution is None and resolution_multiplier <= 0:
        raise click.ClickException("--resolution-multiplier must be > 0 when --resolution is omitted.")
    if resolution is not None and resolution <= 0:
        raise click.ClickException("--resolution must be > 0.")
    if min_files < 1:
        raise click.ClickException("--min-files must be >= 1.")

    cfg = load_config(config)
    base_resolution, target_resolution = _resolve_target_resolution(cfg, resolution, resolution_multiplier)
    embedder = EmbeddingClient(cfg)
    classifier: IntentClassifier | _NoClassifyIntentClassifier
    classifier = _NoClassifyIntentClassifier() if no_classify else IntentClassifier(cfg)

    console.print(f"Reclustering [bold]{repo_name}[/]")
    console.print(f"  Base resolution: {base_resolution:.4f}")
    console.print(f"  Target resolution: {target_resolution:.4f}")

    cluster_conn = get_db(cfg)
    try:
        _assert_repo_exists(cluster_conn, repo_name)
        cluster_count, granularity = materialize_clusters(
            conn=cluster_conn,
            repo_name=repo_name,
            embedder=embedder,
            classifier=classifier,  # type: ignore[arg-type]
            no_classify=no_classify,
            resolution=target_resolution,
        )
    finally:
        cluster_conn.close()

    console.print(f"  Clusters materialized ({granularity}): {cluster_count}")

    if synthesize_logical:
        console.print("  [dim]Refreshing logical modules...[/]")
        synth_conn = get_db(cfg)
        try:
            synthesize_logical_modules(
                conn=synth_conn,
                repo=repo_name,
                min_files=min_files,
                classifier=classifier,  # type: ignore[arg-type]
            )
        finally:
            synth_conn.close()
        console.print("  Logical modules refreshed: yes")
    else:
        console.print("  Logical modules refreshed: no")

    console.print("[bold green]Done.[/]")


if __name__ == "__main__":
    main()
