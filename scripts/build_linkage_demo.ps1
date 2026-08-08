# Build the cross-meeting linkage demo from scratch, into its own database.
#
# Isolated on purpose. The working PostgreSQL database already holds the ACISC
# meeting under a placeholder roster, and its roster guard correctly refuses to
# re-import the same transcript under a different one -- that episode carries
# real accepted work. Rebuilding here instead keeps the demo reproducible and
# leaves live data untouched.
#
# The two meetings are 42 minutes apart on the same evening, which is why they
# share tasks: the second is the follow-up to the first.

# The model runs by default. Without it the deterministic floor finds nothing
# on this pair -- it scores every candidate 0.37-0.50 whether related or not --
# so a run without it produces an empty result that reads like a broken demo.
# -NoModel skips it when only the scores are wanted.
param(
    [string]$Db = "var/linkage-demo.sqlite3",
    [string]$Downloads = "$env:USERPROFILE\Downloads",
    [switch]$NoModel
)

$ErrorActionPreference = "Stop"
$env:PYTHONPATH = "src"
$python = ".venv\Scripts\python.exe"

$firstTranscript = Join-Path $Downloads "20260302215705-ACISC 媒体运营中心第一次例会-逐字稿文本-1.txt"
$secondTranscript = Join-Path $Downloads "20260302223954-转写_王昱翔的快速会议-逐字稿文本-1.txt"
$goldFile = "fixtures/meeting_gold_20260302.json"

foreach ($required in @($firstTranscript, $secondTranscript, $goldFile)) {
    if (-not (Test-Path $required)) { throw "missing: $required" }
}

if (Test-Path $Db) { Remove-Item $Db -Force }

Write-Host "== 1. 校验人工标注 =="
& $python -m collab_agent check-annotation --cases $goldFile
if ($LASTEXITCODE -ne 0) { throw "annotation did not validate" }

Write-Host "`n== 2. 从标注派生抽取（零 token，可复现）=="
& $python -m collab_agent gold-to-extraction `
    --gold $goldFile `
    --output var/extractions/20260302-gold-derived.json

# Imported in meeting order. The candidate pool orders by import time, not by
# meeting date, so the earlier meeting has to go in first for a recorded
# prior_action_item_id to be truthful.
Write-Host "`n== 3. 先载入 21:57 第一次例会 =="
& $python -m collab_agent feishu-serve `
    --extraction var/extractions/acisc-2026-03-02.json `
    --transcript $firstTranscript `
    --organization ACISC --coordinator 王昱翔 `
    --participant 王昱翔 --participant 黄Z恒 --participant 榨椰汁 `
    --participant 宋潽暄 --participant 绒 --participant Jasmine `
    --db $Db --dry-run

Write-Host "`n== 4. 再载入 22:39 快速会议 =="
& $python -m collab_agent feishu-serve `
    --extraction var/extractions/20260302-gold-derived.json `
    --transcript $secondTranscript `
    --organization ACISC --coordinator 王昱翔 `
    --participant 黄Z恒 --participant 王昱翔 --participant 榨椰汁 `
    --participant 张静雅 --participant 绒 --participant 宋潽暄 `
    --db $Db --dry-run

Write-Host "`n== 5. 跨会议关联：字面 vs 语义 =="
$linkArgs = @(
    "-m", "collab_agent", "link", "propose",
    "--db", $Db, "--actor", "王昱翔", "--show-scores"
)
if (-not $NoModel) { $linkArgs += "--with-model" }
# Explicit encoding: `>` defaults to UTF-16LE here, which the reader rejects.
$runFile = Join-Path $env:TEMP "linkage-demo-run.json"
& $python @linkArgs | Out-File -FilePath $runFile -Encoding utf8
if ($LASTEXITCODE -ne 0) { throw "link propose failed" }

& $python scripts/report_linkage_demo.py $runFile

Write-Host "`n演示库: $Db"
Write-Host "查看提议:  $python -m collab_agent link list --db $Db"
Write-Host "确认一条:  $python -m collab_agent link confirm --db $Db --link-id <前几位即可> --actor 王昱翔"
