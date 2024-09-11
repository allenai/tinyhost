import click

@click.command()
@click.argument('html_file')
def tinyhost(html_file):
    """A simple CLI tool that takes an HTML file name as an argument."""
    print("A")

if __name__ == '__main__':
    tinyhost()
