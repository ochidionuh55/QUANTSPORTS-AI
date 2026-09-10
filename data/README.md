# Historical CSV data

Drop season files from [football-data.co.uk](https://www.football-data.co.uk/data.php)
here. Name them `<DIV>_<SEASON>.csv` with the season's slash replaced by a
hyphen:

```
E0_2023-2024.csv
E0_2022-2023.csv
SP1_2023-2024.csv
```

Then run:

```powershell
docker compose exec api python scripts/backtest.py --data /data --competition E0 --season 2023/2024
```

Several seasons at once, which is usually necessary to clear the minimum sample:

```powershell
docker compose exec api python scripts/backtest.py --data /data --competition E0 `
    --season 2021/2022 --season 2022/2023 --season 2023/2024
```

To see how model influence affects skill rather than testing one setting:

```powershell
docker compose exec api python scripts/backtest.py --data /data --competition E0 `
    --season 2022/2023 --season 2023/2024 --sweep
```

## Notes

Pinnacle columns (`PSCH`/`PSCD`/`PSCA`) are the sharp reference and the default
prior. Bet365 (`B365*`) is the soft tradeable side. Older seasons may lack
Pinnacle prices — pass `--reference bet365` or `--reference market_avg` if so.

This directory is git-ignored apart from this file. The data is not ours to
redistribute, and the files are revised in place upstream, so a copy here is a
snapshot rather than a source of truth. Every ingestion records a content hash
so a change upstream is detectable.

## Bundled data

384 season files covering **38 divisions across 20 countries**, 2016/17 to
2026/27, roughly 113,000 matches. Derived from
[xgabora/Club-Football-Match-Data-2000-2025](https://github.com/xgabora/Club-Football-Match-Data-2000-2025),
which compiles football-data.co.uk. Included because football-data.co.uk itself
blocks some networks.

A division is included only when it has at least 1,200 matches with complete
1X2 odds since 2016 — enough for team strengths, Elo and form to mean anything.
Divisions below that threshold are excluded rather than shipped with silent
coverage gaps.

Odds columns are `AvgC*` (market average, the reference prior) and `MaxC*`
(best available price). There are no Pinnacle columns in this source, so
**pass `--reference market_avg`**; the default of `pinnacle` will find nothing.

### Coverage

England (5 tiers) · Scotland (4) · Germany (2) · Spain (2) · Italy (2) ·
France (2) · Netherlands · Belgium · Portugal · Switzerland · Austria ·
Denmark · Sweden · Norway · Finland · Ireland · Poland · Romania · Russia ·
Turkey · Greece · USA · Mexico · Brazil · Argentina · Japan · China
