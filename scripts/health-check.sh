#!/bin/bash
# Health check script for UK House Prices services

set -e

echo "=== UK House Prices Service Health Check ==="
echo "Date: $(date)"
echo

# Check if docker-compose services are running
echo "🐳 Docker Services Status:"
docker-compose ps
echo

# Check database connectivity and data freshness
echo "🗄️  Database Health:"
docker-compose exec -T postgres psql -U prices -d house_prices -c "
SELECT 
    'Data Freshness' as check_type,
    last_transaction_date,
    latest_ingestion,
    total_transactions,
    CASE 
        WHEN latest_ingestion > NOW() - INTERVAL '48 hours' THEN '✅ RECENT'
        WHEN latest_ingestion > NOW() - INTERVAL '7 days' THEN '⚠️  STALE' 
        ELSE '❌ OLD'
    END as status
FROM get_data_freshness();
"
echo

# EPC match coverage (share of sold transactions matched to an EPC certificate -> £/m²)
echo "🏷️  EPC match coverage:"
docker-compose exec -T postgres psql -U prices -d house_prices -tAc "
SELECT '   matched ' || COALESCE(round(100.0*sum(matched_txns)/NULLIF(sum(total_txns),0),1),0) || '% of sales'
       || ' (since 2008: ' || COALESCE(round(100.0*sum(matched_txns) FILTER (WHERE year >= DATE '2008-01-01')
            / NULLIF(sum(total_txns) FILTER (WHERE year >= DATE '2008-01-01'),0),1),0) || '%)'
FROM epc_match_coverage;" 2>/dev/null || echo "   (epc_match_coverage not present — apply 03_epc_schema.sql)"
echo

# Check disk usage
echo "💾 Disk Usage:"
docker-compose exec -T postgres du -sh /var/lib/postgresql/data
echo

# Check Grafana connectivity (host 3001, served under /grafana)
echo "📊 Grafana Status:"
if curl -sf http://localhost:3001/grafana/api/health > /dev/null 2>&1; then
    echo "✅ Grafana is accessible"
else
    echo "❌ Grafana is not responding"
fi
echo

# Check API + analytics agent
echo "🔌 API Status:"
if curl -sf http://localhost:8003/api/health > /dev/null 2>&1; then
    echo "✅ API is accessible"
else
    echo "❌ API is not responding"
fi
if curl -sf http://localhost:8003/api/analysis/tools > /dev/null 2>&1; then
    echo "✅ /ask analytics tools registered"
else
    echo "❌ analytics tools endpoint not responding"
fi
echo

# Check the MCP server (streamable HTTP) exposing the analysis tools at :8004/mcp
echo "🧰 MCP Server:"
MCP_CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://localhost:8004/mcp 2>/dev/null)
if [ "$MCP_CODE" != "000" ]; then
    echo "✅ MCP server reachable at :8004/mcp (HTTP $MCP_CODE)"
else
    echo "❌ MCP server not responding"
fi
echo

# Check the agent's read-only DB role exists and is genuinely read-only
echo "🔒 Agent read-only role:"
docker-compose exec -T postgres psql -U prices -d house_prices -tAc "
SELECT CASE WHEN count(*) = 1 THEN '✅ agent_ro present, default_transaction_read_only=on'
            ELSE '❌ agent_ro missing or not read-only' END
FROM pg_roles r, unnest(r.rolconfig) cfg
WHERE r.rolname = 'agent_ro' AND cfg = 'default_transaction_read_only=on';"
echo

# Check the LLM server (llama.cpp) used by /summarise and /ask
echo "🤖 LLM Server:"
LLM_HOST_DEFAULT="http://localhost:8080"
if curl -sf "${LLM_HOST:-$LLM_HOST_DEFAULT}/v1/models" > /dev/null 2>&1; then
    NCTX=$(curl -s "${LLM_HOST:-$LLM_HOST_DEFAULT}/v1/models" | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"][0].get("meta",{}).get("n_ctx","?"))' 2>/dev/null)
    echo "✅ LLM reachable (n_ctx=${NCTX}; /ask wants >= 8192)"
else
    echo "❌ LLM server not responding"
fi
echo

echo "=== Health Check Complete ==="