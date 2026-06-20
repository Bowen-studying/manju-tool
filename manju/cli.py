"""manju-tool CLI 入口"""

import click


@click.group()
def cli():
    """manju-tool: AI 漫剧生成流水线"""
    pass


@cli.command()
def select():
    """选文：从小说网站抓取当天文章并过滤"""
    click.echo("选文模块（待实现）")


@cli.command()
@click.argument("file", type=click.Path(exists=True))
def clean(file):
    """清洗：文本清洗 + 违禁词审核"""
    click.echo(f"清洗 {file}（待实现）")


@cli.command()
@click.argument("file", type=click.Path(exists=True))
def review(file):
    """审核：AI 深度审核"""
    click.echo(f"审核 {file}（待实现）")


@cli.command()
@click.argument("file", type=click.Path(exists=True))
def rename(file):
    """改名：同音替换角色名"""
    click.echo(f"改名 {file}（待实现）")


@cli.command()
def pipeline():
    """一键全流程"""
    click.echo("全流程（待实现）")


def main():
    cli()


if __name__ == "__main__":
    main()
