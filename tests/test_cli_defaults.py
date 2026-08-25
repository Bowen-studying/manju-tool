from click.testing import CliRunner

from manju.cli import cli


def _command(name):
    return cli.commands[name]


def _option(command, name):
    return next(param for param in command.params if param.name == name)


def test_storyboard_and_pipeline_default_to_agent_engine():
    for command_name in ("storyboard", "pipeline"):
        option = _option(_command(command_name), "engine")
        assert option.default == "agent"
        assert tuple(option.type.choices) == ("legacy", "workflow", "agent")


def test_media_generation_remains_opt_in():
    storyboard = _command("storyboard")
    pipeline = _command("pipeline")

    assert _option(storyboard, "image_api").default is False
    assert _option(storyboard, "image_engine").default == "legacy"
    assert _option(pipeline, "image_api").default is False
    assert _option(pipeline, "image_engine").default == "legacy"
    assert _option(pipeline, "do_speak").default is False
    assert _option(pipeline, "render_videos").default is False
    assert _option(storyboard, "resume").default is False
    assert _option(pipeline, "resume").default is False


def test_cli_help_exposes_the_new_defaults_without_running_media_stages():
    runner = CliRunner()

    for command_name in ("storyboard", "pipeline"):
        result = runner.invoke(cli, [command_name, "--help"])
        assert result.exit_code == 0
        assert "[default: agent]" in result.output


def test_storyboard_default_route_is_offline_and_does_not_resume_old_output(tmp_path, monkeypatch):
    source = tmp_path / "story.txt"
    source.write_text("离线 CLI 路由测试", encoding="utf-8")
    old_output = tmp_path / "old-output"
    old_output.mkdir()
    (old_output / "storyboard.json").write_text("stale", encoding="utf-8")
    calls = []

    def fake_run_storyboard(path, **kwargs):
        calls.append((path, kwargs))
        return {"scenes": [{"shots": []}], "metadata": {}}

    monkeypatch.setattr("manju.cli.run_storyboard", fake_run_storyboard)
    result = CliRunner().invoke(cli, ["storyboard", str(source), "-o", str(old_output)])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][1]["engine"] == "agent"
    assert calls[0][1]["image_api"] is False
    assert calls[0][1]["resume"] is False


def test_pipeline_explicit_legacy_route_stays_offline_by_default(tmp_path, monkeypatch):
    source = tmp_path / "script.json"
    source.write_text("{}", encoding="utf-8")
    output = tmp_path / "pipeline-output"
    calls = {"storyboard": [], "voice": 0, "video": 0, "speak": 0, "render": 0}

    def fake_run_storyboard(path, **kwargs):
        calls["storyboard"].append(kwargs)
        storyboard_dir = kwargs["output_dir"]
        import json
        from pathlib import Path
        Path(storyboard_dir).mkdir(parents=True, exist_ok=True)
        (Path(storyboard_dir) / "storyboard.json").write_text(
            json.dumps({"scenes": [], "metadata": {}}), encoding="utf-8"
        )
        return {"scenes": [], "metadata": {}}

    def fake_run_voice(*args, **kwargs):
        calls["voice"] += 1
        return []

    def fake_run_video(*args, **kwargs):
        calls["video"] += 1
        return []

    monkeypatch.setattr("manju.cli.run_storyboard", fake_run_storyboard)
    monkeypatch.setattr("manju.cli.run_voice", fake_run_voice)
    monkeypatch.setattr("manju.cli.run_video", fake_run_video)
    monkeypatch.setattr("manju.cli.run_batch_speak", lambda *a, **k: calls.__setitem__("speak", calls["speak"] + 1))
    monkeypatch.setattr("manju.cli.run_generate", lambda *a, **k: calls.__setitem__("render", calls["render"] + 1))

    result = CliRunner().invoke(cli, ["pipeline", "--script", str(source), "-o", str(output), "--engine", "legacy"])

    assert result.exit_code == 0, result.output
    assert calls["storyboard"][0]["engine"] == "legacy"
    assert calls["storyboard"][0]["image_api"] is False
    assert calls["storyboard"][0]["resume"] is False
    assert calls["voice"] == 1 and calls["video"] == 1
    assert calls["speak"] == 0 and calls["render"] == 0
