#!/usr/bin/python3

import os
import datetime
import logging
import logging.config
import pprint

import boto3
from botocore.config import Config


# Names of required environment variables
# Add new ones here when used
REQUIRED_ENVVAR = (
    "ECS_REGION",
    "ECS_CLUSTER",
    "ECS_SERVICE",
    "ECS_TASK",
    "ECS_SUBNETS",
    "ECS_SECURITY_GROUPS",

    "RDS_REGION",
    "RDS_CLUSTER_ID",
    "RDS_SNAPSHOT_PREFIX",
)

# "Import" environment variables form os.environ
## IMPORTANT: this raises exception if an env var is missing to detect lacking of required env
env = { name: os.environ[name] for name in REQUIRED_ENVVAR }


logging.config.dictConfig({
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'with_time': {
            'format': '[{asctime}][{levelname}] {message}',
            'style': '{',
        }
    },
    'handlers': {
        'console': {
            'level': 'INFO',
            'class': 'logging.StreamHandler',
            'formatter': 'with_time',
        }
    },
    'loggers': {
        'console': {
            'handlers': [ 'console' ],
            'level': 'INFO',
        },
    }
})

logger = logging.getLogger('console')

format_response = pprint.pformat

# Wait time for certain step, in seconds
SNAPSHOT_DB_WAIT_TIME = 600
MIGRATE_DB_WAIT_TIME = 180
COLLECT_STATIC_WAIT_TIME = 240
DEPLOY_SERVER_WAIT_TIME = 360


rds_client = boto3.client('rds', config=Config(region_name=env['RDS_REGION']))
ecs_client = boto3.client('ecs', config=Config(region_name=env['ECS_REGION']))
log_client = boto3.client('logs', config=Config(region_name=env['ECS_REGION']))


# Exception for when a step fail after some wait
class StepFailed(Exception):
    pass

class TaskFailed(Exception):
    pass


# Create a RDS cluster snapshot
def step_backup_rds():
    snapshot_id_ext = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')
    snapshot_id = "{}-{}".format(env['RDS_SNAPSHOT_PREFIX'], snapshot_id_ext)
    response = rds_client.create_db_cluster_snapshot(
        DBClusterSnapshotIdentifier=snapshot_id,
        DBClusterIdentifier=env['RDS_CLUSTER_ID']
    )
    logger.info("Creating RDS snapshot:\n%s", format_response(response))

    def wait(wait_time=SNAPSHOT_DB_WAIT_TIME):
        rds_client.get_waiter('db_cluster_snapshot_available').wait(
            DBClusterSnapshotIdentifier=snapshot_id,
            WaiterConfig={
                'MaxAttempts': wait_time // 30 + 1  # default polling interval is 30 sec
            }
        )
        logger.info("Snapshot %s created.", snapshot_id)

    return wait


def run_task(overrides):
    subnets = env['ECS_SUBNETS'].split(',')
    sec_groups = env['ECS_SECURITY_GROUPS'].split(',')
    task_network = {
        "awsvpcConfiguration": {
            "subnets": subnets,
            "securityGroups": sec_groups,
            "assignPublicIp": "DISABLED"
        }
    }
    response = ecs_client.run_task(
        cluster=env['ECS_CLUSTER'],
        taskDefinition=env['ECS_TASK'],
        launchType='FARGATE',
        networkConfiguration=task_network,
        overrides=overrides,
        propagateTags='TASK_DEFINITION'
    )
    logger.info("Created tasks:\n%s", format_response(response))
    if response['failures']:
        raise StepFailed(format_response(response['failures']))

    task_arn = response['tasks'][0]['taskArn']
    return task_arn


def wait_task(task_arn, wait_time):
    ecs_client.get_waiter('tasks_stopped').wait(
        cluster=env['ECS_CLUSTER'],
        tasks=[task_arn],
        WaiterConfig={
            'MaxAttempts': wait_time // 6 + 1       # default polling interval is 6 sec
        }
    )


def wait_manage_complete(task_arn, wait_time):
    wait_task(task_arn, wait_time)
    response = ecs_client.describe_tasks(
        cluster=env['ECS_CLUSTER'],
        tasks=[task_arn],
    )
    logger.info("Task detail:\n%s", format_response(response))
    log_group, log_stream = get_container_log_stream_name(env['ECS_TASK'], task_arn, 'django-be')
    logger.info('Container logs:\n%s', '\n'.join(get_cloudwatch_logs(log_group, log_stream)))

    task = response['tasks'][0]
    container = next(filter(lambda c: c['name'] == 'django-be', task['containers']))
    if (response['failures']
        or task['stopCode'] != 'EssentialContainerExited'
        or container['exitCode'] != 0
    ):
        raise TaskFailed()


def get_container_log_stream_name(task_def, task_arn, container):
    task_id = task_arn.split('/')[-1]
    response = ecs_client.describe_task_definition(taskDefinition=task_def)
    cont_def = response['taskDefinition']['containerDefinitions']
    cont_log = next(filter(lambda c: c['name'] == container, cont_def))['logConfiguration']
    if cont_log['logDriver'] == 'awslogs':
        log_group = cont_log['options']['awslogs-group']
        log_stream = '{}/{}/{}'.format(cont_log['options']['awslogs-stream-prefix'], container, task_id)
        return log_group, log_stream
    else:
        return '', ''

def get_cloudwatch_logs(log_group, log_stream):
    response = log_client.get_log_events(
        logGroupName=log_group,
        logStreamName=log_stream
    )
    def format_log(log):
        return "  [{}] {}".format(datetime.datetime.fromtimestamp(log['timestamp'] // 1000).isoformat(), log['message'])

    return map(format_log, response['events'])


# Run migrate database task
def step_migrate_db():
    migrate_db_overrides = {
        "containerOverrides": [
            {
                "name": "django-be",
                "command": ["python", "manage.py", "migrate"]
            }
        ]
    }
    try:
        logger.info('Running task "Migrate Database"')
        task_arn = run_task(migrate_db_overrides)
    except StepFailed:
        raise StepFailed("Migrate Database step failed")

    def wait(wait_time=MIGRATE_DB_WAIT_TIME):
        try:
            wait_manage_complete(task_arn, wait_time)
        except Exception as e:
            raise StepFailed("Migrate Database step failed") from e
        logger.info('Task "Migrate Database" completed.')

    return wait


 # Run collect static task
def step_collect_static():
    collect_static_overrides = {
        "containerOverrides": [
            {
                "name": "django-be",
                "command": ["python", "manage.py", "collectstatic", "--no-input", "--clear"]
            }
        ]
    }
    try:
        logger.info('Running task "Collect Static"')
        task_arn = run_task(collect_static_overrides)
    except StepFailed:
        raise StepFailed("Collect Static step failed")

    def wait(wait_time=COLLECT_STATIC_WAIT_TIME):
        try:
            wait_manage_complete(task_arn, wait_time)
        except Exception as e:
            raise StepFailed("Collect Static step failed") from e
        logger.info('Task "Collect Static" completed.')

    return wait


# Redeploy ECS service with new container image
def step_deploy_service():
    response = ecs_client.update_service(
        cluster=env['ECS_CLUSTER'],
        service=env['ECS_SERVICE'],
        forceNewDeployment=True
    )
    logger.info('Redeploying ECS service for backend\n%s', format_response(response))

    def wait(wait_time=DEPLOY_SERVER_WAIT_TIME):
        ecs_client.get_waiter('services_stable').wait(
            cluster=env['ECS_CLUSTER'],
            services=[env['ECS_SERVICE']],
            WaiterConfig={
                'MaxAttempts': wait_time // 15 + 1          # default polling interval is 15 sec
            }
        )
        logger.info('ECS service redeployed.')

    return wait


# Deployment steps
def main():
    logger.info("Starting deployment...")
    # Backup database in a snapshot, just in case
    wait = step_backup_rds()
    wait()
    # Migrate database to new version
    wait = step_migrate_db()
    wait()
    # Collect static files for admin page to S3
    wait1 = step_collect_static()
    # Force redeployment of ECS service
    wait2 = step_deploy_service()
    wait1()
    wait2()
    logger.info("Deployment completed.")


if __name__ == '__main__':
    main()
