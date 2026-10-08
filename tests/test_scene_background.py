def test_scene_background_is_saved_with_the_scene_and_not_global_identity(app,agent_id):
    scene=app.conversations.create(agent_id,"故事场景",scene="浮岛",background="我们在浮岛图书馆交流。")
    assert scene["background"]=="我们在浮岛图书馆交流。"
    assert scene["scope"].startswith("scene:")
    assert app.identity.current(agent_id).entries=={}
    ordinary=app.conversations.create(agent_id,"日常")
    assert ordinary["background"]==""
