import asyncio

from lingnexus.learning import ConsolidationWorker


def test_background_pause_is_persisted_and_no_work_runs_while_paused(app, tmp_path):
    from lingnexus.background import Supervisor

    async def scenario():
        worker = ConsolidationWorker(app)
        supervisor = Supervisor(app, worker)
        supervisor.configure(enabled=False)
        assert (await supervisor.run_now())["status"] == "paused"
        other = Supervisor(app, worker)
        assert other.status()["enabled"] is False
        await worker.aclose()

    asyncio.run(scenario())


def test_background_missing_credentials_does_not_consume_pending_job(app, agent_id):
    from lingnexus.background import Supervisor

    app.history.add(agent_id, "值得整理的经历", kind="user", scope="real", request_id="background")

    async def scenario():
        worker = ConsolidationWorker(app)
        supervisor = Supervisor(app, worker)
        result = await supervisor.run_now()
        assert result["status"] == "blocked_credentials"
        assert len(app.history.pending_jobs(agent_id)) == 1
        await worker.aclose()

    asyncio.run(scenario())


def test_background_foreground_work_has_priority(app):
    from lingnexus.background import Supervisor

    async def scenario():
        worker = ConsolidationWorker(app)
        supervisor = Supervisor(app, worker, foreground_busy=lambda: True)
        assert (await supervisor.run_now())["status"] == "foreground_busy"
        await worker.aclose()

    asyncio.run(scenario())
