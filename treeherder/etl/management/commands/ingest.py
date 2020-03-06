import asyncio
import inspect
import logging
from concurrent.futures import ThreadPoolExecutor

import aiohttp
import environ
import requests
import taskcluster
import taskcluster.aio
import taskcluster_urls as liburls
from django.conf import settings
from django.core.management.base import BaseCommand

from treeherder.etl.common import fetch_json
from treeherder.etl.db_semaphore import (acquire_connection,
                                         release_connection)
from treeherder.etl.job_loader import JobLoader
from treeherder.etl.push_loader import PushLoader
from treeherder.etl.pushlog import HgPushlogProcess
from treeherder.etl.taskcluster_pulse.handler import (EXCHANGE_EVENT_MAP,
                                                      handleMessage)
from treeherder.model.models import Repository

GITHUB_API = "https://api.github.com"

env = environ.Env()
TOKEN = env("GITHUB_TOKEN", default=None)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

loop = asyncio.get_event_loop()
# Limiting the connection pool just in case we have too many
conn = aiohttp.TCPConnector(limit=10)
# Remove default timeout limit of 5 minutes
timeout = aiohttp.ClientTimeout(total=0)
session = taskcluster.aio.createSession(loop=loop, connector=conn, timeout=timeout)

# Executor to run threads in parallel
executor = ThreadPoolExecutor()

stateToExchange = {}
for key, value in EXCHANGE_EVENT_MAP.items():
    stateToExchange[value] = key


async def handleTaskId(taskId, root_url):
    asyncQueue = taskcluster.aio.Queue({"rootUrl": root_url}, session=session)
    results = await asyncio.gather(asyncQueue.status(taskId), asyncQueue.task(taskId))
    await handleTask({
        "status": results[0]["status"],
        "task": results[1],
    }, root_url)


async def handleTask(task, root_url):
    taskId = task["status"]["taskId"]
    runs = task["status"]["runs"]
    # If we iterate in order of the runs, we will not be able to mark older runs as
    # "retry" instead of exception
    for run in reversed(runs):
        message = {
            "exchange": stateToExchange[run["state"]],
            "payload": {
                "status": {
                    "taskId": taskId,
                    "runs": runs,
                },
                "runId": run["runId"],
            },
            "root_url": root_url,
        }

        try:
            taskRuns = await handleMessage(message, task["task"])
        except Exception as e:
            logger.exception(e)

        if taskRuns:
            # Schedule and run jobs inside the thread pool executor
            jobFutures = [routine_to_future(
                process_job_with_threads, run, root_url) for run in taskRuns]
            await await_futures(jobFutures)


async def fetchGroupTasks(taskGroupId, root_url):
    tasks = []
    query = {}
    continuationToken = ""
    asyncQueue = taskcluster.aio.Queue({"rootUrl": root_url}, session=session)
    while True:
        if continuationToken:
            query = {"continuationToken": continuationToken}
        response = await asyncQueue.listTaskGroup(taskGroupId, query=query)
        tasks.extend(response["tasks"])
        continuationToken = response.get("continuationToken")
        if continuationToken is None:
            break
        logger.info("Requesting more tasks. %s tasks so far...", len(tasks))
    return tasks


async def processTasks(taskGroupId, root_url):
    try:
        tasks = await fetchGroupTasks(taskGroupId, root_url)
        logger.info("We have %s tasks to process", len(tasks))
    except Exception as e:
        logger.exception(e)

    if not tasks:  # No tasks to process
        return

    # Schedule and run tasks inside the thread pool executor
    taskFutures = [routine_to_future(handleTask, task, root_url) for task in tasks]
    await await_futures(taskFutures)


async def routine_to_future(func, *args):
    """Arrange for a function to be executed in the thread pool executor.
    Returns an asyncio.Futures object.
    """
    def _wrap_coroutine(func, *args):
        """Wraps a coroutine into a regular routine to be ran by threads."""
        asyncio.run(func(*args))

    event_loop = asyncio.get_event_loop()
    if inspect.iscoroutinefunction(func):
        return await event_loop.run_in_executor(executor, _wrap_coroutine, func, *args)
    return await event_loop.run_in_executor(executor, func, *args)


async def await_futures(fs):
    """Await for each asyncio.Futures given by fs to copmlete."""
    for fut in fs:
        try:
            await fut
        except Exception as e:
            logger.exception(e)


def process_job_with_threads(pulse_job, root_url):
    acquire_connection()
    logger.info("Loading into DB:\t%s", pulse_job["taskId"])
    JobLoader().process_job(pulse_job, root_url)
    release_connection()


def find_task_id(index_path, root_url):
    index_url = liburls.api(root_url, 'index', 'v1', 'task/{}'.format(index_path))
    response = requests.get(index_url)
    if response.status_code == 404:
        raise Exception("Index URL {} not found".format(index_url))
    return response.json()['taskId']


def get_decision_task_id(project, revision, root_url):
    index_fmt = 'gecko.v2.{}.revision.{}.taskgraph.decision'
    index_path = index_fmt.format(project, revision)
    return find_task_id(index_path, root_url)


def fetchApi(path):
    headers = {}
    if TOKEN:
        headers["Authorization"] = "token ${}".format(TOKEN)

    return fetch_json("{}/{}".format(GITHUB_API, path))


def compareUrl(_repo, base, commit):
    return fetchApi("repos/{}/{}/compare/{}...{}".format(_repo["owner"], _repo["repo"], base, commit))


def repo_meta(project):
    _repo = Repository.objects.filter(name=project)[0]
    assert _repo, "The project {} you specified is incorrect".format(project)
    splitUrl = _repo.url.split("/")
    return {
        "url": _repo.url,
        "branch": _repo.branch,
        "owner": splitUrl[3],
        "repo": splitUrl[4],
        "tc_root_url": _repo.tc_root_url,
    }


def query_data(repo_meta, commit):
    # This is used for the `compare` API. The "event.base.sha" is only contained in Pulse events, thus,
    # we need to determine the correct value
    eventBaseSha = repo_meta["branch"]
    # e.g. https://api.github.com/repos/servo/servo/compare/master...1418c0555ff77e5a3d6cf0c6020ba92ece36be2e
    compareResponse = compareUrl(repo_meta, repo_meta["branch"], commit)
    # headCommit = None
    mergeBaseCommit = compareResponse.get("merge_base_commit")
    if mergeBaseCommit:
        logger.info("We have a merge commit. We need to find the right eventBaseSha")
        # Since we don't use PushEvents that contain the "before" or "event.base.sha" fields [1]
        # we need to discover the right parent. A merge commit has two parents
        # [1] https://github.com/taskcluster/taskcluster/blob/3dda0adf85619d18c5dcf255259f3e274d2be346/services/github/src/api.js#L55
        parents = compareResponse["merge_base_commit"]["parents"]
        for parent in parents:
            _commit = fetch_json(parent["url"])
            if _commit["parents"]:
                eventBaseSha = parent["sha"]
                logger.info("We have a new base: %s", eventBaseSha)
                break
        # When using the correct eventBaseSha the "commits" field will be correct
        compareResponse = compareUrl(repo_meta, eventBaseSha, commit)

    commits = []
    for _commit in compareResponse["commits"]:
        commits.append({
            "message": _commit["commit"]["message"],
            "author": _commit["commit"]["author"],
            "committer": _commit["commit"]["committer"],
            "id": _commit["sha"],
        })

    return eventBaseSha, commits


def github_push_to_pulse(repo_meta, commit):
    event_base_sha, commits = query_data(repo_meta, commit)

    return {
        "exchange": "exchange/taskcluster-github/v1/push",
        "routingKey": "primary.{}.{}".format(repo_meta["owner"], repo_meta["repo"]),
        "payload": {
            "organization": repo_meta["owner"],
            "details": {
                "event.head.repo.url": "{}.git".format(repo_meta["url"]),
                "event.base.repo.branch": repo_meta["branch"],
                "event.base.sha": event_base_sha,
                "event.head.sha": commit,
            },
            "body": {
                "commits": commits,
            },
            "repository": repo_meta["repo"]
        }
    }


def ingestGitPush(project, commit):
    _repo = repo_meta("fenix")
    pulse = github_push_to_pulse(_repo, commit["sha"])
    PushLoader().process(pulse["payload"], pulse["exchange"], _repo["tc_root_url"])


def ingestGitPushes(project="fenix"):
    _repo = repo_meta(project)
    commits = fetchApi("repos/{}/{}/commits".format(_repo["owner"], _repo["repo"]))
    for _commit in commits:
        ingestGitPush(project, _commit)


class Command(BaseCommand):
    """Management command to ingest data from a single push."""
    help = "Ingests a single push and tasks into Treeherder"

    def add_arguments(self, parser):
        parser.add_argument(
            "ingestion_type",
            nargs=1,
            help="Type of ingestion to do: [task|hg-push|git-commit|pr]"
        )
        parser.add_argument(
            "-p", "--project",
            help="Hg repository to query (e.g. autoland)"
        )
        parser.add_argument(
            "-c", "--commit", "-r", "--revision",
            help="Commit/revision to import"
        )
        parser.add_argument(
            "--enable-eager-celery",
            action="store_true",
            help="This will cause all Celery queues to execute (like log parsing). This will take way longer."
        )
        parser.add_argument(
            "-a", "--ingest-all-tasks",
            action="store_true",
            help="This will cause all tasks associated to a commit to be ingested. This can take a long time."
        )
        parser.add_argument(
            "--root-url",
            dest="root_url",
            default="https://firefox-ci-tc.services.mozilla.com",
            help="Taskcluster root URL for non-Firefox tasks (e.g. https://community-tc.services.mozilla.com"
        )
        parser.add_argument(
            "--task-id",
            dest="taskId",
            nargs="?",
            help="taskId to ingest"
        )
        parser.add_argument(
            "--pr-url",
            dest="prUrl",
            help="Ingest a PR: e.g. https://github.com/mozilla-mobile/android-components/pull/4821"
        )

    def handle(self, *args, **options):
        typeOfIngestion = options["ingestion_type"][0]
        root_url = options["root_url"]

        if typeOfIngestion == "task":
            assert options["taskId"]
            loop.run_until_complete(handleTaskId(options["taskId"], root_url))
        elif typeOfIngestion == "pr":
            assert options["prUrl"]
            pr_url = options["prUrl"]
            splitUrl = pr_url.split("/")
            org = splitUrl[3]
            repo = splitUrl[4]
            pulse = {
                "exchange": "exchange/taskcluster-github/v1/pull-request",
                "routingKey": "primary.{}.{}.synchronize".format(org, repo),
                "payload": {
                    "repository": repo,
                    "organization": org,
                    "action": "synchronize",
                    "details": {
                        "event.pullNumber": splitUrl[6],
                        "event.base.repo.url": "https://github.com/{}/{}.git".format(org, repo),
                        "event.head.repo.url": "https://github.com/{}/{}.git".format(org, repo),
                    },
                }
            }
            PushLoader().process(pulse["payload"], pulse["exchange"], root_url)
        elif typeOfIngestion.find("git") > -1:
            if not TOKEN:
                logger.warning("If you don't set up GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET you can hit Github's rate limitting.")

            if typeOfIngestion == "git-push":
                ingestGitPush(options["project"], options["commit"])
            elif typeOfIngestion == "git-pushes":
                ingestGitPushes()
        elif typeOfIngestion == "push":
            if not options["enable_eager_celery"]:
                logger.info(
                    "If you want all logs to be parsed use --enable-eager-celery"
                )
            else:
                # Make sure all tasks are run synchronously / immediately
                settings.CELERY_TASK_ALWAYS_EAGER = True

            # get reference to repo and ingest this particular revision for this project
            project = options["project"]
            commit = options["commit"]
            repo = Repository.objects.get(name=project, active_status="active")
            pushlog_url = "%s/json-pushes/?full=1&version=2" % repo.url
            process = HgPushlogProcess()
            process.run(pushlog_url, project, changeset=commit, last_push_id=None)

            if options["ingest_all_tasks"]:
                gecko_decision_task = get_decision_task_id(project, commit, repo.tc_root_url)
                logger.info("## START ##")
                loop.run_until_complete(processTasks(gecko_decision_task, repo.tc_root_url))
                logger.info("## END ##")
            else:
                logger.info(
                    "You can ingest all tasks for a push with -a/--ingest-all-tasks."
                )
