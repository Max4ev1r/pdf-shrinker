#!/bin/bash
HA_TOKEN=$(cat /Users/max/.hermes/secrets/ha_token.txt)
curl -s -X POST \
  -H "Authorization: Bearer $HA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"entity_id":"fan.xiaomi_cn_731457187_m19_s_14_air_fresh"}' \
  http://localhost:8123/api/services/fan/turn_on
