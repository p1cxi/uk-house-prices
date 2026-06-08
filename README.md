# UK House Prices Data Pipeline

A self-hosted data pipeline that ingests HM Land Registry Price Paid Data into PostgreSQL and visualizes it through Grafana dashboards. Built for tracking residential property prices in target areas with automated monthly updates.

<img width="1597" height="952" alt="image" src="https://github.com/user-attachments/assets/c49e9b05-7daf-46f4-a1e5-901172046d1e" />

<img width="1598" height="955" alt="image" src="https://github.com/user-attachments/assets/0d8a10c6-9654-4187-baa2-93630a7ae3be" />

## Architecture

```
HM Land Registry API → FastAPI Service → PostgreSQL 16 → Grafana Dashboard
                    ↓        ↓             ↑
                ONS API  llama.cpp     Materialized Views
                         (Summary)    & Indexes
```

## Features

- **REST API interface**: HTTP endpoints for all ingestion and analysis operations
- **Complete historical data**: Ingest full Land Registry dataset (1995-present)
- **Automated updates**: Monthly delta processing with proper A/C/D record handling
- **Gap detection & backfill**: Automatically detect missing months and re-ingest affected years
- **AI-powered summaries**: Generate plain English market briefings via a local llama.cpp server
- **Geographic filtering**: Target specific counties for focused AI summaries and analysis
- **Performance optimized**: Materialized views and strategic indexes for fast queries
- **Rich visualizations**: Time series analysis, geographic heatmaps, market statistics
- **Data quality**: Validates transactions, handles edge cases, structured logging

## Quick Start

### 1. Prerequisites

- Docker and Docker Compose
- 10GB+ free disk space (for filtered dataset)
- Reliable internet connection (initial download ~5GB)

### 2. Setup

```bash
# Clone/create project directory
cd {whatever-dir-you-want}

git clone https://github.com/sord-dev/uk-house-prices.git

# Copy environment template
cp .env.example .env

# Edit configuration
nano .env
```

### 3. Configure Environment

Edit `.env` with your settings:

```bash
# Database credentials
POSTGRES_PASSWORD=your_secure_password_here
GRAFANA_ADMIN_PASSWORD=admin_password_here

# Target areas for AI summaries (comma-separated, uppercase)
TARGET_COUNTIES=ESSEX,HERTFORDSHIRE,KENT,SURREY,CAMBRIDGESHIRE

# AI service configuration (optional) — llama.cpp OpenAI-compatible server
LLM_HOST=http://localhost:8080
LLM_MODEL=Qwen3-8B-Q4_K_M.gguf
```

### 4. Start Services

```bash
# Start all services (database, API, Grafana)
docker-compose up -d

# Wait for services to initialize (30-60 seconds)
docker-compose logs -f

# Verify services are healthy
docker-compose ps
```

### 5. Initial Data Load

```bash
# Full historical dataset (takes 30-60 minutes)
curl -X POST http://localhost:8003/api/ingest/yearly?year=2023

# Check ingestion status
curl http://localhost:8003/api/ingest/yearly_2023/status

# Or load multiple years
for year in 2022 2023 2024; do
  curl -X POST "http://localhost:8003/api/ingest/yearly?year=$year"
done
```

### 6. Access Services

**Grafana Dashboard**: `http://localhost:3001`
- Username: `admin`
- Password: (from your `.env` file)

**API Documentation**: `http://localhost:8003/api/docs`
- Interactive API explorer
- Test endpoints directly

**Health Check**: `http://localhost:8003/api/health`

## Usage

### API Endpoints

```bash
# Monthly updates (run monthly after 20th working day)
curl -X POST http://localhost:8003/api/ingest/monthly

# Yearly data ingestion
curl -X POST "http://localhost:8003/api/ingest/yearly?year=2024"

# Check which months have data and whether any gaps exist
curl http://localhost:8003/api/ingest/coverage

# Backfill any missing months (re-ingests the affected years)
curl -X POST http://localhost:8003/api/ingest/backfill

# Check job status
curl http://localhost:8003/api/ingest/monthly/status
curl http://localhost:8003/api/ingest/yearly_2024/status
curl http://localhost:8003/api/ingest/backfill/status

# Generate AI summary of recent market data
curl -X POST http://localhost:8003/api/summarise/monthly

# Health check
curl http://localhost:8003/api/health
```

### AI Market Summaries

Generate plain English briefings suitable for push notifications, alongside structured data:

```bash
curl -X POST http://localhost:8003/api/summarise/monthly
```

Response shape:

```json
{
  "summary": "Our March 2026 market update reports 1,766 transactions across 8 monitored areas, with Kent leading at 571 transactions. The top areas by volume are Kent (£330,000), Essex (£375,000), and Hertfordshire (£450,000). Bexley saw the largest year-on-year gain at +15.7% YoY.",
  "data_period": "April 2026",
  "actual_date_range": { "from": "2026-03-01", "to": "2026-03-31" },
  "areas_analysed": 8,
  "data": {
    "top_by_volume": [...],
    "top_gainers": [...],
    "top_fallers": [...],
    "top_yoy_gainers": [...],
    "top_yoy_fallers": [...]
  }
}
```

**`data_period`** is the nominal reporting month (e.g. "April 2026"). **`actual_date_range`** shows the real transaction dates in the data — due to Land Registry registration lag, these will typically be 4–8 weeks behind the nominal period. Responses are cached per reporting month and invalidated automatically on ingest.

The `data` block contains pre-selected notable areas (top by transaction volume, biggest MoM and YoY movers) for use in dashboards or notifications without re-querying. Pull `.summary` for the push notification text.

### Automation

Schedule monthly updates with cron:

```bash
# Add to crontab (crontab -e)
# Run monthly update on 21st at 6 AM
0 6 21 * * curl -X POST http://localhost:8003/api/ingest/monthly

# Generate and send AI summary after ingestion
30 6 21 * * curl -X POST http://localhost:8003/api/summarise/monthly | jq -r '.summary' | your-notification-system
```

Or use n8n for more complex workflows:
- HTTP Request: `POST http://house_prices_api:8000/ingest/monthly`
- Wait for completion: Poll `/ingest/monthly/status` until complete
- HTTP Request: `POST http://house_prices_api:8000/summarise/monthly` 
- Send notification with summary text

### Monitoring

```bash
# Check API service logs
docker-compose logs api

# Monitor running jobs
curl http://localhost:8003/api/ingest/monthly/status

# Database status
docker-compose exec postgres psql -U prices -d house_prices -c "SELECT * FROM get_data_freshness();"

# Service health
curl http://localhost:8003/api/health
docker-compose ps
```

### Maintenance

```bash
# Refresh materialized views manually
docker-compose exec postgres psql -U prices -d house_prices -c "SELECT refresh_monthly_stats();"

# Backup database
docker-compose exec postgres pg_dump -U prices house_prices > backup_$(date +%Y%m%d).sql

# Update container images
docker-compose pull
docker-compose up -d
```

## Configuration

### Geographic Targeting

Edit `TARGET_COUNTIES` in `.env` to focus AI summaries on specific areas:

```bash
# Example: Focus on London commuter belt  
TARGET_COUNTIES=HERTFORDSHIRE,ESSEX,KENT,SURREY

# Example: Expand to wider South East
TARGET_COUNTIES=HERTFORDSHIRE,ESSEX,KENT,SURREY,BUCKINGHAMSHIRE,BERKSHIRE,OXFORDSHIRE
```

**Note**: All transactions are ingested regardless of `TARGET_COUNTIES`. The filter only applies to AI summary generation and the `/summarise/monthly` query, letting you focus notifications on areas of interest while keeping the full dataset available for Grafana dashboards and ad-hoc queries.

Restart the API service after changing counties:
```bash
docker-compose restart api
```

### Data Volume Sizing

Expected storage requirements (PostgreSQL + indexes):

| Scope | Transactions | Storage |
|-------|-------------|---------|
| Single county, 5 years | ~500K | ~200MB |
| 5 counties, full history | ~5M | ~2GB |
| England & Wales, full history | ~28M | ~15GB |

Mount point in docker-compose.yml should use fast storage (NVMe/SSD).

## Database Schema

### Key Tables

- `transactions` - Main price paid data with full Land Registry schema
- `postcodes` - ONS postcode directory for geocoding
- `market_transactions` - Clean view excluding non-standard sales
- `monthly_price_stats` - Pre-aggregated statistics for performance

### Example Queries

```sql
-- Median prices by county, last 12 months
SELECT 
    county,
    COUNT(*) as transactions,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY price) as median_price
FROM market_transactions 
WHERE date >= CURRENT_DATE - INTERVAL '12 months'
GROUP BY county
ORDER BY median_price DESC;

-- Monthly trend for specific area
SELECT 
    DATE_TRUNC('month', date) as month,
    property_type,
    COUNT(*) as count,
    median_price
FROM monthly_price_stats
WHERE county = 'HERTFORDSHIRE'
  AND month >= '2023-01-01'
ORDER BY month, property_type;
```

## Data Sources & Licensing

### HM Land Registry Price Paid Data
- **License**: Open Government Licence v3.0
- **Coverage**: England & Wales, 1995-present  
- **Updates**: Monthly on 20th working day
- **Lag**: 2 weeks to 2 months for registration
- **URL**: https://landregistry.data.gov.uk/

### ONS Postcode Directory
- **License**: Open Government Licence v3.0
- **Purpose**: Lat/long coordinates for mapping
- **Updates**: Quarterly
- **URL**: https://geoportal.statistics.gov.uk/

## Troubleshooting

### Common Issues

**Services won't start**
```bash
# Check logs for errors
docker-compose logs postgres grafana

# Verify .env file is present and valid
cat .env

# Check port conflicts
ss -tlpn | grep -E ":3001|:8003|:5433"
```

**API/Ingestion fails**
```bash
# Check API service logs
docker-compose logs api

# Test API health
curl http://localhost:8003/api/health

# Check database connectivity
docker-compose exec postgres psql -U prices -d house_prices -c "\dt"

# Verify Land Registry URLs are accessible
curl -I https://price-paid-data.publicdata.landregistry.gov.uk/pp-monthly-update-new-version.csv

# Check available disk space
df -h
```

**Grafana connection issues**
```bash
# Verify datasource configuration
docker-compose exec grafana cat /etc/grafana/provisioning/datasources/postgres.yml

# Test database connection
docker-compose exec postgres psql -U prices -h postgres -d house_prices -c "SELECT COUNT(*) FROM transactions;"
```

### Performance Tuning

**Slow queries**
```sql
-- Check index usage
SELECT schemaname, tablename, indexname, idx_scan, idx_tup_read 
FROM pg_stat_user_indexes 
ORDER BY idx_scan DESC;

-- Analyze table statistics  
ANALYZE transactions;
ANALYZE monthly_price_stats;
```

**Large dataset optimization**
```sql
-- Partition large tables by date (for advanced users)
-- Consider archiving old data beyond analysis timeframe
-- Adjust PostgreSQL memory settings in docker-compose.yml
```

## Development

### Local Development

```bash
# Install Python dependencies
cd ingest
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run tests
python -m pytest tests/

# Lint code
python -m flake8 ingest.py
```

### Contributing

1. Follow existing code structure and patterns
2. Add tests for new functionality  
3. Update documentation for any schema changes
4. Test with representative data samples

## License

This project is licensed under MIT License. 

Land Registry and ONS data are provided under Open Government Licence v3.0.

## Support

For issues with this pipeline:
- Check logs: `docker-compose logs`
- Review troubleshooting section above
- Check database connectivity and disk space

For data issues:
- Land Registry: https://landregistry.data.gov.uk/
- ONS Postcode: https://geoportal.statistics.gov.uk/
