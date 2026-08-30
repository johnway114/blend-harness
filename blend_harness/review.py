"""Self-contained, build-directory-independent static review packages."""

from __future__ import annotations

import html
import json
import shutil
from pathlib import Path
from typing import Any, Callable

from .errors import BlendError, ErrorCategory
from .project import Project
from .util import atomic_write_json, atomic_write_text, canonical_json, load_json, sha256_bytes, sha256_file, utc_now


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

def _check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled and cancelled():
        raise BlendError(
            code="PROCESS_INTERRUPTED",
            category=ErrorCategory.INTERRUPTED,
            message="Review package generation was interrupted; incomplete staging was discarded.",
            remediation="Retry with a new operation identifier.",
        )



def _copy_artifact(source: Path, assets: Path) -> dict[str, Any]:
    checksum = sha256_file(source)
    destination = assets / f"{checksum[:12]}-{source.name}"
    if not destination.exists():
        shutil.copy2(source, destination)
    return {
        "path": f"assets/{destination.name}",
        "sha256": checksum,
        "bytes": destination.stat().st_size,
    }


def _collect_manifests(project: Project) -> list[dict[str, Any]]:
    values = []
    if project.paths.artifacts.is_dir():
        for path in sorted(project.paths.artifacts.glob("*.json")):
            try:
                value = load_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if (
                value.get("schema") == 1
                and value.get("operation")
                and value.get("blendVersion")
                and value.get("inputs")
            ):
                values.append({"path": path, "value": value})
    return values

def _collect_comparisons(project: Project) -> list[dict[str, Any]]:
    records = []
    if not project.paths.working.is_dir():
        return records
    for path in sorted(project.paths.working.rglob("*.json")):
        if not (
            path.name == "comparison.json"
            or path.name.startswith("search-")
            or "comparison" in path.stem
        ):
            continue
        try:
            value = load_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("schema") == 1:
            records.append({"path": path, "value": value})
    return records


def _collect_images(project: Project) -> list[Path]:
    roots = [project.paths.previews, project.paths.renders, project.paths.outputs, project.paths.working]
    images = []
    for root in roots:
        if root.is_dir():
            images.extend(path for path in sorted(root.rglob("*")) if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES)
    return list(dict.fromkeys(images))


def _collect_deliverables(
    project: Project,
    manifests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    records = []
    included_paths: set[Path] = set()
    portable_suffixes = {".mp4", ".mov", ".webm", ".mkv", ".json"}
    for item in manifests:
        manifest = item["value"]
        for output in manifest.get("outputs", []):
            path = Path(output.get("path", ""))
            if not path.is_absolute():
                path = project.paths.root / path
            path = path.resolve()
            if path in included_paths or not path.is_file() or path.suffix.lower() not in portable_suffixes:
                continue
            included_paths.add(path)
            records.append({
                "source": path,
                "operation": manifest.get("operation"),
                "profile": manifest.get("resolved", {}).get("profileName"),
                "variant": manifest.get("resolved", {}).get("variantName"),
                "kind": output.get("kind"),
                "codec": output.get("codec"),
                "width": manifest.get("resolved", {}).get("profile", {}).get("width"),
                "height": manifest.get("resolved", {}).get("profile", {}).get("height"),
                "frameRate": manifest.get("resolved", {}).get("frameRate"),
                "frameStart": manifest.get("resolved", {}).get("frameStart"),
                "frameEnd": manifest.get("resolved", {}).get("frameEnd"),
                "warnings": manifest.get("warnings", []),
                "overrides": manifest.get("overrides", []),
            })
    return records


def _render_html(data: dict[str, Any]) -> str:
    payload = json.dumps(data, sort_keys=True).replace("</", "<\\/")
    project_id = html.escape(data["project"]["id"])
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{project_id} · Blend review ledger</title>
<style>
:root {{
  --ink: #11100d;
  --coal: #181713;
  --paper: #e8e2d4;
  --muted: #999181;
  --line: #373229;
  --amber: #e69a36;
  --amber-pale: #f3c47f;
  --error: #ee6b55;
  --ok: #89a779;
  --serif: "Iowan Old Style", "Baskerville", "Palatino Linotype", serif;
  --mono: "IBM Plex Mono", "Menlo", "Consolas", monospace;
}}
* {{ box-sizing: border-box; }}
html {{ background: var(--ink); color: var(--paper); font-family: var(--serif); }}
body {{ margin: 0; min-height: 100vh; background:
  radial-gradient(circle at 82% -10%, rgba(230,154,54,.13), transparent 38rem),
  repeating-linear-gradient(90deg, transparent 0 79px, rgba(255,255,255,.018) 80px), var(--ink); }}
body::before {{ content:""; position:fixed; inset:0; pointer-events:none; opacity:.28; background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 140 140' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='.06'/%3E%3C/svg%3E"); }}
header {{ min-height: 62vh; display:grid; grid-template-columns:minmax(0,1.5fr) minmax(18rem,.7fr); gap:4vw; padding:5rem max(5vw,2rem) 3rem; border-bottom:1px solid var(--line); align-items:end; }}
.eyebrow, .meta, button, label, select, textarea, .chip, th {{ font-family:var(--mono); text-transform:uppercase; letter-spacing:.12em; }}
.eyebrow {{ color:var(--amber); font-size:.72rem; margin-bottom:1.5rem; }}
h1 {{ font-weight:400; font-size:clamp(4rem,11vw,10rem); letter-spacing:-.07em; line-height:.76; margin:0; max-width:11ch; }}
.dek {{ font-size:clamp(1.15rem,2.1vw,1.65rem); color:#c7bfaf; max-width:35rem; line-height:1.42; margin:2.4rem 0 0; }}
.stamp {{ border:1px solid var(--amber); padding:1.4rem; position:relative; transform:rotate(-1.2deg); background:rgba(230,154,54,.035); }}
.stamp::after {{ content:"REVIEW COPY"; color:var(--amber); font-family:var(--mono); letter-spacing:.24em; position:absolute; right:-.8rem; top:-.7rem; background:var(--ink); padding:.2rem .5rem; font-size:.65rem; }}
.meta {{ display:grid; grid-template-columns:7rem 1fr; gap:.65rem 1rem; font-size:.72rem; line-height:1.45; }}
.meta dt {{ color:var(--muted); }} .meta dd {{ margin:0; word-break:break-word; }}
nav {{ position:sticky; top:0; z-index:4; display:flex; gap:1.4rem; overflow:auto; padding:.9rem max(5vw,2rem); background:rgba(17,16,13,.88); backdrop-filter:blur(18px); border-bottom:1px solid var(--line); }}
nav a {{ color:var(--paper); text-decoration:none; font-family:var(--mono); font-size:.68rem; text-transform:uppercase; letter-spacing:.12em; white-space:nowrap; }} nav a:hover {{ color:var(--amber); }}
main {{ padding:0 max(5vw,2rem) 8rem; }}
section {{ padding:6rem 0 2rem; border-bottom:1px solid var(--line); }}
.section-head {{ display:grid; grid-template-columns:5rem minmax(0,1fr); gap:2rem; margin-bottom:3rem; }}
.index {{ font-family:var(--mono); color:var(--amber); font-size:.8rem; }}
h2 {{ font-size:clamp(2.5rem,6vw,5.5rem); font-weight:400; letter-spacing:-.055em; line-height:.9; margin:0; }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(12rem,1fr)); border:1px solid var(--line); }}
.stat {{ padding:1.4rem; min-height:8rem; border-right:1px solid var(--line); }} .stat:last-child {{ border:0; }}
.stat strong {{ display:block; font-size:2.7rem; font-weight:400; }} .stat span {{ color:var(--muted); font: .68rem var(--mono); text-transform:uppercase; letter-spacing:.1em; }}
.gallery {{ display:grid; grid-template-columns:repeat(12,1fr); gap:1.4rem; }}
.plate {{ grid-column:span 6; margin:0; background:var(--coal); border:1px solid var(--line); }} .plate:nth-child(3n) {{ grid-column:span 12; }}
.plate img, .plate video {{ width:100%; display:block; background:#0a0a09; max-height:75vh; object-fit:contain; }}
.plate figcaption {{ display:grid; grid-template-columns:1fr auto; gap:1rem; padding:1rem; color:var(--muted); font:.68rem var(--mono); }}
.finding-tools {{ display:flex; gap:1rem; margin-bottom:1rem; flex-wrap:wrap; }}
select, textarea {{ color:var(--paper); background:var(--coal); border:1px solid var(--line); padding:.8rem; }}
table {{ width:100%; border-collapse:collapse; font-size:.94rem; }} th {{ color:var(--muted); text-align:left; font-size:.63rem; }} th, td {{ border-bottom:1px solid var(--line); padding:1rem .65rem; vertical-align:top; }}
.chip {{ display:inline-block; font-size:.58rem; padding:.28rem .42rem; border:1px solid currentColor; }} .error {{ color:var(--error); }} .warning {{ color:var(--amber-pale); }} .info, .passed {{ color:var(--ok); }}
.acceptance {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(18rem,1fr)); gap:1rem; }} .acceptance article {{ padding:1.3rem; border-left:2px solid var(--amber); background:var(--coal); }}
.acceptance p {{ margin:0; font-size:1.2rem; }} .acceptance small {{ display:block; color:var(--muted); margin-top:.8rem; font-family:var(--mono); }}
.ledger {{ font-family:var(--mono); font-size:.72rem; max-height:30rem; overflow:auto; border:1px solid var(--line); }} .ledger div {{ display:grid; grid-template-columns:10rem 1fr 8rem; gap:1rem; padding:.8rem; border-bottom:1px solid var(--line); }}
.disposition {{ display:grid; grid-template-columns:minmax(14rem,.55fr) minmax(18rem,1fr); gap:2rem; }} textarea {{ width:100%; min-height:12rem; resize:vertical; text-transform:none; letter-spacing:normal; }}
button {{ background:var(--amber); color:#16120d; border:0; padding:1rem 1.4rem; cursor:pointer; font-size:.68rem; font-weight:700; }} button:hover {{ background:var(--amber-pale); }}
footer {{ padding:2rem max(5vw,2rem); color:var(--muted); font:.65rem var(--mono); }}
.reveal {{ animation:rise .7s both; animation-delay:calc(var(--order,0)*70ms); }} @keyframes rise {{ from {{ opacity:0; transform:translateY(20px); }} }}
@media(max-width:760px) {{ header {{ grid-template-columns:1fr; min-height:auto; padding-top:4rem; }} .plate,.plate:nth-child(3n) {{ grid-column:span 12; }} .disposition {{ grid-template-columns:1fr; }} .section-head {{ grid-template-columns:2rem 1fr; }} .ledger div {{ grid-template-columns:1fr; }} }}
@media(prefers-reduced-motion:reduce) {{ .reveal {{ animation:none; }} }}
</style>
</head>
<body>
<header>
  <div class="reveal"><div class="eyebrow">Blend artifact review · immutable evidence</div><h1>{project_id}</h1><p class="dek">A portable review ledger for source, scene, validation, comparisons, and rendered evidence. Structural checks do not certify aesthetic intent.</p></div>
  <aside class="stamp reveal" style="--order:2"><dl class="meta" id="project-meta"></dl></aside>
</header>
<nav><a href="#evidence">Evidence</a><a href="#findings">Findings</a><a href="#acceptance">Acceptance</a><a href="#provenance">Provenance</a><a href="#disposition">Disposition</a></nav>
<main>
<section id="evidence"><div class="section-head"><span class="index">01</span><h2>Visual evidence</h2></div><div class="stats" id="stats"></div><div class="gallery" id="gallery"></div></section>
<section id="findings"><div class="section-head"><span class="index">02</span><h2>Mechanical findings</h2></div><div class="finding-tools"><label>Severity <select id="severity"><option value="all">All</option><option>error</option><option>warning</option><option>info</option></select></label></div><table><thead><tr><th>Severity</th><th>Rule</th><th>Finding and evidence</th><th>Disposition</th></tr></thead><tbody id="findings-body"></tbody></table></section>
<section id="acceptance"><div class="section-head"><span class="index">03</span><h2>Review statements</h2></div><div class="acceptance" id="acceptance-list"></div></section>
<section id="provenance"><div class="section-head"><span class="index">04</span><h2>Artifact ledger</h2></div><div class="ledger" id="ledger"></div></section>
<section id="disposition"><div class="section-head"><span class="index">05</span><h2>Record disposition</h2></div><div class="disposition"><div><label>Decision<select id="decision"><option value="approved">Approved</option><option value="changes-requested">Changes requested</option><option value="selected">Selected variant</option><option value="no-decision">No decision</option></select></label><br><br><label>Selected variant<select id="variant"></select></label></div><div><label>Comments<textarea id="comments" placeholder="Review only. Source remains untouched."></textarea></label><br><button id="download">Download signed review record</button></div></div></section>
</main>
<footer>Generated by Blend · offline static package · checksums are the review identity.</footer>
<script>
const data={payload};
const $=s=>document.querySelector(s);
const esc=value=>String(value??'').replace(/[&<>"']/g,ch=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
const meta=[['Created',data.createdAt],['Source',data.project.sourceRevision||'unversioned'],['Profile',data.summary.profile||'mixed'],['Variant',data.summary.variant||'base'],['Blender',data.summary.blenderVersion||'unknown'],['Package',data.packageChecksum||'computed on record']];
$('#project-meta').innerHTML=meta.map(([k,v])=>`<dt>${{esc(k)}}</dt><dd>${{esc(v)}}</dd>`).join('');
const counts=data.validation?.summary||{{errors:0,warnings:0,info:0,passed:true}};
const media=(data.deliverables||[]).filter(x=>/\\.(mp4|mov|webm|mkv)$/i.test(x.path));
$('#stats').innerHTML=[[data.images.length,'images'],[media.length,'videos'],[data.manifests.length,'manifests'],[counts.errors,'errors'],[counts.warnings,'warnings']].map(([n,l],i)=>`<div class="stat reveal" style="--order:${{i}}"><strong>${{esc(n)}}</strong><span>${{esc(l)}}</span></div>`).join('');
const imagePlates=data.images.map((x,i)=>`<figure class="plate reveal" style="--order:${{i%6}}"><img loading="lazy" src="${{esc(x.path)}}" alt="Review artifact ${{i+1}}"><figcaption><span>${{esc(x.label)}}</span><span>${{esc(x.sha256.slice(0,12))}}</span></figcaption></figure>`);
const videoPlates=media.map((x,i)=>`<figure class="plate reveal" style="--order:${{i%6}}"><video controls preload="metadata" src="${{esc(x.path)}}"></video><figcaption><span>${{esc(x.kind||'video')}} · ${{esc(x.width)}}×${{esc(x.height)}} · ${{esc(x.frameRate)}} fps</span><span>${{esc(x.sha256.slice(0,12))}}</span></figcaption></figure>`);
$('#gallery').innerHTML=[...imagePlates,...videoPlates].join('')||'<p>No visual evidence was retained.</p>';
function findings(){{const severity=$('#severity').value;const rows=(data.validation?.findings||[]).filter(x=>severity==='all'||x.severity===severity);$('#findings-body').innerHTML=rows.map(x=>`<tr><td><span class="chip ${{esc(x.severity)}}">${{esc(x.severity)}}</span></td><td><code>${{esc(x.ruleId)}}</code></td><td>${{esc(x.message)}}<br><small>${{esc(JSON.stringify(x.evidence))}}</small></td><td>${{x.suppressed?'suppressed: '+esc(x.suppressionReason):'active'}}</td></tr>`).join('')||'<tr><td colspan="4" class="passed">No matching active findings.</td></tr>';}}
$('#severity').addEventListener('change',findings);findings();
$('#acceptance-list').innerHTML=(data.acceptance||[]).map(x=>`<article><p>${{esc(x)}}</p><small>Subjective review statement · not mechanically certified</small></article>`).join('');
$('#ledger').innerHTML=data.artifacts.map(x=>`<div><span>${{esc(x.sha256.slice(0,12))}}</span><span>${{esc(x.path)}}</span><span>${{esc(x.bytes)}} bytes</span></div>`).join('');
const variants=['',...data.variants];$('#variant').innerHTML=variants.map(x=>`<option value="${{esc(x)}}">${{esc(x||'none')}}</option>`).join('');
$('#download').addEventListener('click',()=>{{const record={{schema:1,project:data.project,reviewPackageChecksum:data.packageChecksum||null,decision:$('#decision').value,selectedVariant:$('#variant').value||null,comments:$('#comments').value,reviewedArtifacts:data.artifacts.map(x=>({{path:x.path,sha256:x.sha256}})),createdAt:new Date().toISOString()}};const blob=new Blob([JSON.stringify(record,null,2)+'\\n'],{{type:'application/json'}});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='review-disposition.json';a.click();URL.revokeObjectURL(a.href);}});
</script>
</body></html>'''


def _build_review_package(
    project: Project,
    destination: Path,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    assets_dir = destination / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    manifests = _collect_manifests(project)
    manifest_records = []
    copied_artifacts: list[dict[str, Any]] = []
    for manifest in manifests:
        _check_cancelled(cancelled)
        copied = _copy_artifact(manifest["path"], assets_dir)
        copied_artifacts.append(copied)
        manifest_records.append({"artifact": copied, "value": manifest["value"]})
    images = []
    for path in _collect_images(project):
        _check_cancelled(cancelled)
        copied = _copy_artifact(path, assets_dir)
        copied_artifacts.append(copied)
        try:
            label = path.relative_to(project.paths.root).as_posix()
        except ValueError:
            label = str(path)
        images.append({**copied, "label": label})
    validation = (
        load_json(project.paths.validation)
        if project.paths.validation.is_file()
        else {
            "summary": {"errors": 0, "warnings": 0, "info": 0, "passed": False},
            "findings": [],
        }
    )
    if project.paths.validation.is_file():
        copied_artifacts.append(_copy_artifact(project.paths.validation, assets_dir))
    comparison_records = []
    for comparison in _collect_comparisons(project):
        _check_cancelled(cancelled)
        copied = _copy_artifact(comparison["path"], assets_dir)
        copied_artifacts.append(copied)
        comparison_records.append({"artifact": copied, "value": comparison["value"]})
    deliverables = []
    for record in _collect_deliverables(project, manifests):
        _check_cancelled(cancelled)
        copied = _copy_artifact(record.pop("source"), assets_dir)
        copied_artifacts.append(copied)
        deliverables.append({**copied, **record})
    copied_artifacts = list({
        item["path"]: item for item in copied_artifacts
    }.values())
    latest = max(
        (item["value"] for item in manifests),
        key=lambda value: (
            value.get("timing", {}).get("endedAt")
            or value.get("timing", {}).get("startedAt", "")
        ),
        default={},
    )
    data = {
        "schema": 1,
        "createdAt": utc_now(),
        "project": {
            "id": project.id,
            "sourceRevision": latest.get("project", {}).get("sourceRevision"),
        },
        "summary": {
            "profile": latest.get("resolved", {}).get("profileName"),
            "variant": latest.get("resolved", {}).get("variantName"),
            "blenderVersion": latest.get("blender", {}).get("version"),
        },
        "variants": sorted(project.config.get("variants", {})),
        "acceptance": project.brief.get("acceptance", []),
        "validation": validation,
        "images": images,
        "manifests": manifest_records,
        "comparisons": comparison_records,
        "artifacts": copied_artifacts,
        "deliverables": deliverables,
        "packageChecksum": None,
    }
    identity_inventory = sorted(
        (
            {"path": item["path"], "sha256": item["sha256"], "bytes": item["bytes"]}
            for item in copied_artifacts
        ),
        key=lambda item: item["path"],
    )
    data["packageChecksum"] = sha256_bytes(canonical_json(identity_inventory))
    _check_cancelled(cancelled)
    data_path = destination / "review-data.json"
    atomic_write_json(data_path, data)
    index_path = destination / "index.html"
    atomic_write_text(index_path, _render_html(data))
    inventory = [
        {
            "path": path.relative_to(destination).as_posix(),
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }
        for path in sorted(destination.rglob("*"))
        if path.is_file() and path.name != "package-manifest.json"
    ]
    package = {
        "schema": 1,
        "project": project.id,
        "createdAt": data["createdAt"],
        "packageId": data["packageChecksum"],
        "files": inventory,
    }
    package_path = destination / "package-manifest.json"
    atomic_write_json(package_path, package)
    return {
        "path": str(destination),
        "index": str(index_path),
        "manifest": str(package_path),
        "files": len(package["files"]) + 1,
        "portable": True,
    }


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def create_review_package(
    project: Project,
    destination: Path,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    final_destination = destination.expanduser().resolve()
    generated_roots = (
        project.paths.working,
        project.paths.previews,
        project.paths.renders,
        project.paths.outputs,
    )
    if (
        _within(final_destination, project.paths.root)
        and not any(_within(final_destination, root) for root in generated_roots)
    ):
        raise BlendError(
            code="REVIEW_DESTINATION_UNSAFE",
            category=ErrorCategory.REVIEW,
            message="Review package destination overlaps authoritative project source.",
            remediation="Choose a path outside the project or under a declared generated root.",
        )
    if final_destination.exists() and not (final_destination / "package-manifest.json").is_file():
        raise BlendError(
            code="REVIEW_DESTINATION_NOT_OWNED",
            category=ErrorCategory.REVIEW,
            message=f"Existing destination is not a Blend-owned review package: {final_destination}",
            remediation="Choose an empty destination; Blend will not delete unrelated files.",
        )
    if (final_destination / "review-disposition.json").is_file():
        raise BlendError(
            code="REVIEW_DISPOSITION_RETAINED",
            category=ErrorCategory.REVIEW,
            message="The existing review package contains a disposition record.",
            remediation="Choose a new destination so reviewed evidence remains immutable.",
            retained_artifacts=[str(final_destination / "review-disposition.json")],
        )
    final_destination.parent.mkdir(parents=True, exist_ok=True)
    stage = final_destination.with_name(f".{final_destination.name}.part")
    backup = final_destination.with_name(f".{final_destination.name}.previous")
    shutil.rmtree(stage, ignore_errors=True)
    shutil.rmtree(backup, ignore_errors=True)
    try:
        built = _build_review_package(project, stage, cancelled=cancelled)
        _check_cancelled(cancelled)
        if final_destination.exists():
            final_destination.replace(backup)
        stage.replace(final_destination)
        shutil.rmtree(backup, ignore_errors=True)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        if backup.exists() and not final_destination.exists():
            backup.replace(final_destination)
        raise
    return {
        **built,
        "path": str(final_destination),
        "index": str(final_destination / "index.html"),
        "manifest": str(final_destination / "package-manifest.json"),
    }


def record_disposition(package: Path, *, decision: str, comments: str,
                       selected_variant: str | None) -> dict[str, Any]:
    package = package.expanduser().resolve()
    disposition_path = package / "review-disposition.json"
    if disposition_path.exists():
        raise BlendError(
            code="REVIEW_DISPOSITION_EXISTS",
            category=ErrorCategory.REVIEW,
            message="This review package already has a retained disposition.",
            remediation="Create a new review package for a subsequent decision.",
            retained_artifacts=[str(disposition_path)],
        )
    review_data_path = package / "review-data.json"
    review_data = load_json(review_data_path) if review_data_path.is_file() else {}
    if selected_variant and selected_variant not in review_data.get("variants", []):
        raise BlendError(
            code="REVIEW_VARIANT_UNKNOWN",
            category=ErrorCategory.REVIEW,
            message=f"Selected variant {selected_variant!r} is not present in this review package.",
            remediation="Choose a variant embedded in the reviewed evidence.",
        )
    manifest_path = package / "package-manifest.json"
    if not manifest_path.is_file():
        raise BlendError(
            code="REVIEW_PACKAGE_INVALID",
            category=ErrorCategory.REVIEW,
            message=f"Review package manifest is missing: {manifest_path}",
            remediation="Pass a complete review package generated by blend review.",
        )
    package_manifest = load_json(manifest_path)
    for item in package_manifest.get("files", []):
        artifact = package / item["path"]
        if not artifact.is_file() or sha256_file(artifact) != item["sha256"]:
            raise BlendError(
                code="REVIEW_PACKAGE_TAMPERED",
                category=ErrorCategory.REVIEW,
                message=f"Review package artifact failed checksum validation: {item['path']}",
                remediation="Regenerate the review package before recording a disposition.",
            )
    record = {
        "schema": 1,
        "project": package_manifest.get("project"),
        "reviewPackageChecksum": package_manifest.get("packageId") or sha256_file(manifest_path),
        "decision": decision,
        "selectedVariant": selected_variant,
        "comments": comments,
        "reviewedArtifacts": package_manifest["files"],
        "createdAt": utc_now(),
        "sourceModified": False,
    }
    destination = disposition_path
    atomic_write_json(destination, record)
    return {"path": str(destination), "sha256": sha256_file(destination), "record": record}
