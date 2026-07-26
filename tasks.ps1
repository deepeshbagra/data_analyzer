<#
.SYNOPSIS
    Windows shim for the Makefile targets. GNU make is not installed on this host.

.DESCRIPTION
    Every target here dispatches to the SAME docker compose commands as the
    equivalent Makefile target. If you change one, change the other -- drift
    between them is a bug, not a convenience.

.EXAMPLE
    .\tasks.ps1 up
    .\tasks.ps1 test
    .\tasks.ps1 revision -Message "add correction table"
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('help', 'up', 'down', 'logs', 'migrate', 'revision', 'psql', 'shell',
                 'test', 'test-fast', 'lint', 'fmt', 'typecheck', 'check', 'reset')]
    [string]$Target = 'help',

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Rest,

    [string]$Message
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# These two take arguments via the automatic $args and deliberately declare NO
# param() block. A [Parameter()] attribute would make them advanced functions,
# which adds PowerShell's common parameters -- and the binder then swallows any
# docker flag that prefix-matches one. `up -d` silently became `up` (-d -> -Debug)
# and `down -v` silently became `down` (-v -> -Verbose), so `up` ran attached and
# hung, and `reset` preserved the volumes it exists to destroy. See DECISIONS #21.
function Invoke-Compose {
    Write-Host "docker compose $($args -join ' ')" -ForegroundColor DarkGray
    & docker compose @args
    if ($LASTEXITCODE -ne 0) { throw "docker compose failed with exit code $LASTEXITCODE" }
}

# run --rm --no-deps: for lint/typecheck we do not want to boot the database.
function Invoke-Tool {
    Invoke-Compose run --rm --no-deps api @args
}

function Initialize-EnvFile {
    if (-not (Test-Path '.env')) {
        Copy-Item '.env.example' '.env'
        Write-Host 'Created .env from .env.example' -ForegroundColor Yellow
    }
}

switch ($Target) {
    'help' {
        @'
Targets (mirror of the Makefile):

  up          Start the full local stack and apply migrations
  down        Stop the stack (volumes preserved)
  logs        Tail api + worker logs
  migrate     Apply migrations to head
  revision    Autogenerate a migration:  .\tasks.ps1 revision -Message "add foo"
  psql        Open psql as the schema owner
  shell       Python shell inside the api image
  test        Run the full test suite against a live database
  test-fast   Run only tests that do not need a database
  lint        ruff check + format check
  fmt         Apply ruff formatting and import fixes
  typecheck   mypy strict
  check       lint + typecheck + test
  reset       DESTRUCTIVE: drop all local volumes and rebuild
'@ | Write-Host
    }

    'up' {
        Initialize-EnvFile
        Invoke-Compose up -d db redis minio
        Invoke-Compose up minio-init
        Invoke-Compose run --rm migrate
        Invoke-Compose up -d api worker web
        Write-Host ''
        Write-Host 'api   -> http://localhost:8000/docs' -ForegroundColor Green
        Write-Host 'web   -> http://localhost:3000'      -ForegroundColor Green
        Write-Host 'minio -> http://localhost:9001'      -ForegroundColor Green
    }

    'down'      { Invoke-Compose down }
    'logs'      { Invoke-Compose logs -f api worker }
    'migrate'   { Invoke-Compose run --rm migrate }

    'revision' {
        if (-not $Message) { throw 'Provide a message: .\tasks.ps1 revision -Message "add foo"' }
        Invoke-Compose run --rm migrate alembic revision --autogenerate -m $Message
    }

    'psql'  { Invoke-Compose exec db sh -c 'psql -U $POSTGRES_USER -d $POSTGRES_DB' }
    'shell' { Invoke-Compose run --rm api python }

    'test' {
        Initialize-EnvFile
        Invoke-Compose up -d db redis
        Invoke-Compose run --rm migrate
        Invoke-Compose run --rm api pytest @Rest
    }

    'test-fast' { Invoke-Tool pytest -m 'not requires_db' @Rest }

    'lint' {
        Invoke-Tool ruff check .
        Invoke-Tool ruff format --check .
    }

    'fmt' {
        Invoke-Tool ruff check --fix .
        Invoke-Tool ruff format .
    }

    'typecheck' { Invoke-Tool mypy api worker tests }

    'check' {
        & $PSCommandPath lint
        & $PSCommandPath typecheck
        & $PSCommandPath test
    }

    'reset' {
        Write-Host 'This destroys all local database, redis and minio data.' -ForegroundColor Red
        $confirm = Read-Host 'Type RESET to continue'
        if ($confirm -ne 'RESET') { Write-Host 'Aborted.'; return }
        Invoke-Compose down -v
        if (Test-Path '.data') { Remove-Item -Recurse -Force '.data' }
        & $PSCommandPath up
    }
}
