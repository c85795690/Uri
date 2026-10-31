YAlgo V2.6 PostgreSQL paper radar

1. Create a PostgreSQL database (Neon or another hosted PostgreSQL provider).
2. Copy the provider's full connection string, including sslmode=require when supplied.
3. GitHub repository > Settings > Secrets and variables > Actions > New repository secret.
   Name: DATABASE_URL; value: full connection string. Do not put it in code.
4. Keep DISCORD_WEBHOOK secret.
5. Replace root main.py and requirements.txt; replace .github/workflows/radar.yml.
6. Disable the older YAlgo workflow if it is a separate YAML file to avoid duplicate scans.
7. Actions > YAlgo Auto Trader PRO V2.6 PostgreSQL > Run workflow.
8. Test with SCAN_LIMIT: '20', then set to '0' for full scan.

PostgreSQL tables positions and signals are created automatically on first run.
Existing SQLite historical trades are NOT migrated automatically.
The implementation is PAPER ONLY. No live exchange orders are sent.
Note: OHLC-based exits are simulated and do not account for intrabar order or slippage.
