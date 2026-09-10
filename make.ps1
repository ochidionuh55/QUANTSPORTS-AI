<#
.SYNOPSIS
    QUANTSPORT AI task runner for Windows PowerShell.

.DESCRIPTION
    Windows has no `make`, so this script provides the same commands.
    Run it from the project root.

.EXAMPLE
    .\make.ps1 help
    .\make.ps1 up
    .\make.ps1 check
#>

param(
    [Parameter(Position = 0)]
    [string]$Task = "help"
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Write-Ok($message)   { Write-Host "OK  $message" -ForegroundColor Green }
function Write-Warn($message) { Write-Host "!!  $message" -ForegroundColor Yellow }

function Get-Python {
    foreach ($candidate in @("python", "py -3.12", "python3")) {
        $exe, $exeArgs = $candidate -split " ", 2
        if (Get-Command $exe -ErrorAction SilentlyContinue) { return $candidate }
    }
    throw "Python 3.12+ not found on PATH. Install it from python.org."
}

function Invoke-Python {
    param([string[]]$Arguments)
    $py = Get-Python
    $exe, $prefix = $py -split " ", 2
    $all = @()
    if ($prefix) { $all += $prefix }
    $all += $Arguments
    & $exe @all
    if ($LASTEXITCODE -ne 0) { throw "Command failed: $exe $all" }
}

function Test-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker not found. Install Docker Desktop from docker.com and start it."
    }
    docker info 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker is installed but not running. Start Docker Desktop and retry."
    }
}

function Initialize-Env {
    if (-not (Test-Path ".env")) {
        Copy-Item ".env.example" ".env"
        Write-Ok "Created .env from template. Open it and fill in your values."
    }
}

switch ($Task.ToLower()) {

    "help" {
        Write-Host ""
        Write-Host "QUANTSPORT AI - available tasks" -ForegroundColor White
        Write-Host ""
        Write-Host "  Stack:"
        Write-Host "    .\make.ps1 up         Build and start the full stack"
        Write-Host "    .\make.ps1 up-infra   Start only Postgres and Redis"
        Write-Host "    .\make.ps1 down       Stop the stack"
        Write-Host "    .\make.ps1 clean      Stop and delete volumes (DESTROYS DATA)"
        Write-Host "    .\make.ps1 logs       Tail all service logs"
        Write-Host "    .\make.ps1 ps         Show service status"
        Write-Host "    .\make.ps1 health     Query the API health endpoint"
        Write-Host ""
        Write-Host "  Database:"
        Write-Host "    .\make.ps1 migrate      Apply all migrations"
        Write-Host "    .\make.ps1 migration    Autogenerate a new migration"
        Write-Host "    .\make.ps1 migrate-down Roll back one migration"
        Write-Host "    .\make.ps1 db-shell     Open psql"
        Write-Host ""
        Write-Host "  Quality gate:"
        Write-Host "    .\make.ps1 install    Install Python dependencies"
        Write-Host "    .\make.ps1 check      Lint + typecheck + tests"
        Write-Host "    .\make.ps1 test       Run tests"
        Write-Host "    .\make.ps1 coverage   Tests with coverage report"
        Write-Host "    .\make.ps1 lint       Ruff check and format check"
        Write-Host "    .\make.ps1 format     Auto-fix and format"
        Write-Host "    .\make.ps1 typecheck  Mypy strict"
        Write-Host ""
    }

    "env"      { Initialize-Env }

    "install"  {
        Write-Step "Installing dependencies"
        Invoke-Python @("-m", "pip", "install", "-r", "requirements-dev.txt")
        Write-Ok "Dependencies installed."
    }

    "up" {
        Test-Docker
        Initialize-Env
        Write-Step "Building and starting the stack"
        docker compose up --build -d
        if ($LASTEXITCODE -ne 0) { throw "docker compose failed." }
        Write-Ok "Stack started."
        Write-Host "    API:  http://localhost:8000/health"
        Write-Host "    Docs: http://localhost:8000/docs"
    }

    "up-infra" {
        Test-Docker
        Initialize-Env
        Write-Step "Starting Postgres and Redis only"
        docker compose up -d postgres redis
        Write-Ok "Infrastructure started."
    }

    "down"  { Test-Docker; docker compose down }

    "clean" {
        Test-Docker
        Write-Warn "This deletes all local database and Redis data."
        $confirm = Read-Host "Type 'yes' to continue"
        if ($confirm -eq "yes") { docker compose down -v; Write-Ok "Volumes removed." }
        else { Write-Host "Cancelled." }
    }

    "logs"  { Test-Docker; docker compose logs -f }
    "ps"    { Test-Docker; docker compose ps }

    "health" {
        try {
            $response = Invoke-RestMethod -Uri "http://localhost:8000/health" -TimeoutSec 5
            $response | ConvertTo-Json -Depth 5
        } catch {
            Write-Warn "API not reachable on localhost:8000. Is the stack running?"
        }
    }

    "migrate" {
        Test-Docker
        docker compose exec api alembic upgrade head
    }

    "migration" {
        Test-Docker
        $message = Read-Host "Migration message"
        docker compose exec api alembic revision --autogenerate -m $message
    }

    "migrate-down" {
        Test-Docker
        docker compose exec api alembic downgrade -1
    }

    "db-shell" {
        Test-Docker
        docker compose exec postgres psql -U quantsport -d quantsport
    }

    "test"      { Invoke-Python @("-m", "pytest") }
    "coverage"  { Invoke-Python @("-m", "pytest", "--cov=app", "--cov-report=term-missing") }
    "typecheck" { Invoke-Python @("-m", "mypy", "app") }

    "lint" {
        Invoke-Python @("-m", "ruff", "check", ".")
        Invoke-Python @("-m", "ruff", "format", "--check", ".")
    }

    "format" {
        Invoke-Python @("-m", "ruff", "check", "--fix", ".")
        Invoke-Python @("-m", "ruff", "format", ".")
    }

    "check" {
        Write-Step "Lint"
        Invoke-Python @("-m", "ruff", "check", ".")
        Invoke-Python @("-m", "ruff", "format", "--check", ".")
        Write-Step "Type check"
        Invoke-Python @("-m", "mypy", "app")
        Write-Step "Tests"
        Invoke-Python @("-m", "pytest")
        Write-Ok "Quality gate passed."
    }

    default {
        Write-Warn "Unknown task '$Task'. Run '.\make.ps1 help' for the list."
        exit 1
    }
}
