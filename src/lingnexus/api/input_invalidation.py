"""Cancel admitted calls after the owner's source mutation commits."""


async def cancel_inputs(request, *, agent_id=None, conversation_id=None):
    runtime = getattr(request.app.state, "chat_runtime", None)
    if runtime is None:
        return
    with request.app.state.services.database.transaction() as db:
        rows = db.execute("SELECT id,agent_id,conversation_id FROM chat_runs WHERE status='running'").fetchall()
    for row in rows:
        if (agent_id is None or row["agent_id"] == agent_id) and (
            conversation_id is None or row["conversation_id"] == conversation_id
        ):
            await runtime.cancel(row["id"])
