# Synthetic CLI walkthrough

I use this disposable PowerShell example to exercise storage, corrections, recall and retention. I require Python 3.12+ and the tokenizer dependencies from the repository root. The database and exports in this example are plaintext and must contain synthetic data only.

```powershell
$python = 'python'
$root = Join-Path ([IO.Path]::GetTempPath()) ('memorycore-ai-synthetic-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $root | Out-Null
$db = Join-Path $root 'fixture.sqlite3'
$scope = 'project:synthetic'
function Memory { & $python 'scripts/memorycore_ai.py' --db $db @args; if ($LASTEXITCODE -ne 0) { throw 'Synthetic command failed' } }
Memory capabilities
Memory init
$stage = Memory stage --scope $scope --source synthetic --text 'Synthetic decision source.' | ConvertFrom-Json
$saved = Memory consolidate --scope $scope --type semantic --subject Decision --summary 'Synthetic deployment uses Sydney.' --keywords 'deployment region' --source synthetic --stage-id $stage.stage_id | ConvertFrom-Json
$corrected = Memory remember --scope $scope --type semantic --subject Decision --summary 'Synthetic deployment uses Adelaide.' --keywords 'deployment region' --source synthetic --supersedes $saved.memory_id | ConvertFrom-Json
$exact = Memory store-exact --scope $scope --source synthetic --text 'Synthetic exact original.' --user-confirmed --linked-memory-id $corrected.memory_id | ConvertFrom-Json
Memory recall 'deployment region' --scope $scope --max-tokens 700 --reserve-tokens 32 --max-chars 2500 --include-ids
Memory recall 'deployment region' --scope $scope --format json --max-tokens 700
Memory recall-exact $exact.archive_id --scope $scope --offset 0 --length 9
Memory inspect $corrected.memory_id --scope $scope
Memory inspect $exact.archive_id --scope $scope
Memory inspect $stage.stage_id --scope $scope
Memory list --scope $scope --kind semantic --limit 10 --offset 0
Memory list --scope $scope --kind exact
Memory list --scope $scope --kind stage --status consolidated
Memory pin $corrected.memory_id --scope $scope
Memory unpin $corrected.memory_id --scope $scope
Memory retention $corrected.memory_id --scope $scope --expires '2099-01-01T00:00:00Z'
Memory forget $corrected.memory_id --scope $scope
Memory restore $corrected.memory_id --scope $scope
Memory archive $corrected.memory_id --scope $scope
Memory unarchive $corrected.memory_id --scope $scope
Memory retention $corrected.memory_id --scope $scope --until-user-deletes
Memory expire --scope $scope
Memory stats --scope $scope
$export = Join-Path $root 'scope.json'
Memory export --scope $scope --output $export
$restored = Join-Path $root 'restored.sqlite3'
& $python 'scripts/memorycore_ai.py' --db $restored import --scope $scope --file $export --user-confirmed
$review = Memory prune --scope $scope --older-than-days 0 --importance-below 1 | ConvertFrom-Json
foreach ($candidate in $review.candidates) { Memory prune --scope $scope --older-than-days 0 --importance-below 1 --reviewed-id $candidate.review_token --apply --user-confirmed }
Memory purge $exact.archive_id --scope $scope --user-confirmed
Memory purge $stage.stage_id --scope $scope --user-confirmed
Memory migrate
```
