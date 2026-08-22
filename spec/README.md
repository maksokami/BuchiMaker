# BuchiMaker

BuchiMaker is an API-first web application for visual data dashboards, utilizing DuckDB for efficient in-memory data processing and a modular pluggable data source system.

Use database file (e.g. ./db/duck.db) for the DuckDB file
Client: DBeaver

Load csv
CREATE VIEW final_vuln AS SELECT * FROM '/home/onosan/Documents/Scripts/llm-databoom-v0.3/data/final_vuln.csv';

SELECT COUNT(*) FROM final_vuln; - all

docker compose rm -s -f -v backend && docker compose up -d --build backend


docker compose rm -f && docker compose up -d --build


docker build -t buchi-frontend /home/onosan/Documents/Scripts/llm-databoom-v0.3/frontend 2>&1 | tail -6
docker rm -f buchi-frontend-app 2>&1
docker run -d --name buchi-frontend-app -p 8080:80 -e API_BASE_URL=http://localhost:8000/api/v1 buchi-frontend 2>&1
docker rm -f buchi-frontend-app
docker image rm buchi-frontend





SELECT snapshot_date, 
    SUM(new_open_count) as new_findings,
    SUM(total_open_eod) as open_at_end_of_day,
    SUM(newly_closed_count) as closed_on_day,
    SUM(total_overdue_count) as overdue_at_end_of_day
    FROM vuln_daily
    WHERE snapshot_date >= CURRENT_DATE - INTERVAL '90 days'
    GROUP BY snapshot_date
    ORDER BY snapshot_date DESC