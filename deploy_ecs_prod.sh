#!/bin/bash
set -euo pipefail

TASK_OVERRIDE_JSON_COLLECT_STATIC='{
    "containerOverrides": [
        {
            "name": "django-be",
            "command": ["python", "manage.py", "collectstatic", "--no-input", "--clear"]
        }
    ]
}'

TASK_OVERRIDE_JSON_MIGRATE_DB='{
    "containerOverrides": [
        {
            "name": "django-be",
            "command": ["python", "manage.py", "migrate"]
        }
    ]
}'

# run ECS task with custom command
runTask() {
    if [ "$#" -lt 1 ]
    then
        return 1
    fi

    local SUBNET_LIST
    SUBNET_LIST=$(printf "%s" "$ECS_SUBNETS" | sed 's/\([0-9a-zA-Z-]\+\)/"\1"/g')
    local SECGROUP_LIST
    SECGROUP_LIST=$(printf "%s" "$ECS_SECURITY_GROUPS" | sed 's/\([0-9a-zA-Z-]\+\)/"\1"/g')
    local TASK_NETWORK_JSON='{
        "awsvpcConfiguration": {
            "subnets": [ '"$SUBNET_LIST"' ],
            "securityGroups": [ '"$SECGROUP_LIST"' ],
            "assignPublicIp": "DISABLED"
        }
    }'

    aws --region "$ECS_REGION" \
        ecs run-task \
        --task-definition "$ECS_TASK" \
        --cluster "$ECS_CLUSTER" \
        --launch-type "FARGATE" \
        --network-configuration "$TASK_NETWORK_JSON" \
        --overrides "$1" \
        --propagate-tags "TASK_DEFINITION"
}

# Backup DB
SNAPSHOT_EXTRA_ID="$(date -u +%Y%m%d-%H%M%S)"
SNAPSHOT_ID="$RDS_SNAPSHOT_PREFIX-$SNAPSHOT_EXTRA_ID"
aws --region "$RDS_REGION" \
    rds create-db-cluster-snapshot \
    --db-cluster-identifier "$RDS_CLUSTER_ID" \
    --db-cluster-snapshot-identifier "$SNAPSHOT_ID"

# Check backup completion
#TODO
sleep 10
aws --region "$RDS_REGION" \
    rds describe-db-cluster-snapshots \
    --db-cluster-snapshot-identifier "$SNAPSHOT_ID" \
        | jq '.DBClusterSnapshots[0].Status'

# Migrate DB
runTask "$TASK_OVERRIDE_JSON_MIGRATE_DB"

# Check migration completion
#TODO
sleep 5


# Collect static files
runTask "$TASK_OVERRIDE_JSON_COLLECT_STATIC"

# Check collect static completion
#TODO
sleep 5

# Redeploy ECS service
aws --region "$ECS_REGION" ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" --force-new-deployment

# Check service deployment completion
#TODO
