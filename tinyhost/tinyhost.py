import hashlib
import os
import boto3
import click
import tempfile
import secrets
import string
import magic
import json
import re

from bs4 import BeautifulSoup

from botocore.exceptions import NoCredentialsError, ClientError

# Create an S3 client using boto3
s3_client = boto3.client('s3')

@click.command()
@click.option("--bucket", help="S3 bucket on which to host your static site", required=True)
@click.option("--prefix", help="S3 bucket prefix to use", default="")
@click.option("--duration", default=604800, help="Length of time in seconds that this resulting link will work for. Default is 1 week. Max is also 1 week.")
@click.argument("html_file", type=click.Path(exists=True))
def tinyhost(html_file: str, bucket: str, prefix: str, duration: int):
    """
    Hosts your html_file on an S3 bucket, and gives back a signed URL.
    """
    try:
        # Make sure that your file content is a text/html page to begin with
        file_extension = os.path.splitext(html_file)[-1]

        if file_extension.lower() not in [".htm", ".html"]:
            raise click.ClickException("You must use a .htm or .html extension")

        mime = magic.Magic(mime=True)
        content_type = mime.from_file(html_file)

        if content_type != "text/html":
            raise click.ClickException("Your file was not detected as text/html.")
        
        with open(html_file, "r") as f:
            html_content = f.read()
        
        soup = BeautifulSoup(html_content, "html.parser")

        head_tag = soup.find("head")

        # Write or update the datastore section
        if not head_tag:
            raise click.ClickException("Could not find a <head> tag in your html, you'll need to add one")
        
        script_tags = head_tag.find_all("script")
        found_existing_template = False

        for script_tag in script_tags:
            if script_tag.string and "BEGIN TINYHOST DATASTORE SECTION" in script_tag.string:
                datastore_id = re.search(r"const datastoreId = \"(\w+)\";", script_tag.string)[1]

                click.echo(f"Found existing datastore section, replacing...")

                get_url, put_url = get_datastore_presigned_urls(bucket, prefix, datastore_id, duration)
                script_tag.string = get_datastore_section(datastore_id, get_url, put_url)
                found_existing_template = True
                break

        if not found_existing_template:
            click.echo("Need to write in new script template")
            new_script = soup.new_tag("script")

            datastore_id = ''.join(secrets.choice(string.ascii_letters + string.digits) for i in range(20))

            get_url, put_url = get_datastore_presigned_urls(bucket, prefix, datastore_id, duration)
            new_script.string = get_datastore_section(datastore_id, get_url, put_url)
            head_tag.append(new_script)

        html_content = str(soup)

        # Write the data back to the file
        with open(html_file, "w") as f:
            f.write(html_content)

        sha1_hash = compute_sha1_hash(html_file)
        
        new_file_name = f"{sha1_hash}{file_extension}"

        s3_key = f"{prefix}/{new_file_name}" if prefix else new_file_name
        upload_file_to_s3(html_file, bucket, s3_key)

        signed_url = create_presigned_url(bucket, s3_key, expiration=duration)

        if signed_url:
            click.echo(f"Your file has been uploaded successfully!\nAccess it via the following signed URL:\n\n{signed_url}")
        else:
            click.echo("Failed to generate a signed URL.")

    except NoCredentialsError:
        click.echo("AWS credentials not found. Please configure them.")


def get_datastore_section(datastore_id: str, presigned_get_url: str, presigned_put_url: str) -> str:
    with open(os.path.join(os.path.dirname(__file__), "datastore_template.html"), "r") as f:
        template = f.read()

    assert template.find("\"{{ datastore_id }}\"") != -1
    assert template.find("\"{{ presigned_get_url }}\"") != -1
    assert template.find("\"{{ presigned_put_url }}\"") != -1

    template = template.replace("{{ datastore_id }}", datastore_id)
    template = template.replace("{{ presigned_get_url }}", presigned_get_url)
    template = template.replace("{{ presigned_put_url }}", presigned_put_url)

    return template

def get_datastore_presigned_urls(bucket: str, prefix: str, datastore_id: str, duration: int) -> tuple[str, str]:
    object_key = f"{prefix}/{datastore_id}.json"

    # Check if object key exists, if not, make one, with the content {}
    # and the right ContentType
    try:
        s3_client.head_object(Bucket=bucket, Key=object_key)
        print(f"Object {object_key} exists.")
    except ClientError as e:
        if e.response['Error']['Code'] == '404':
            print(f"datastore {object_key} does not exist. Creating it.")
            empty_json = json.dumps({})
            s3_client.put_object(Bucket=bucket, Key=object_key, Body=empty_json, ContentType='application/json')
        else:
            raise e
   
    get_url = s3_client.generate_presigned_url('get_object',
                                                Params={'Bucket': bucket, 'Key': object_key},
                                                ExpiresIn=duration)  
    
    put_url = s3_client.generate_presigned_url('put_object',
                                                Params={'Bucket': bucket, 'Key': object_key, 'ContentType': 'application/json'},
                                                ExpiresIn=duration) 
    
    return get_url, put_url

def compute_sha1_hash(file_path: str) -> str:
    sha1 = hashlib.sha1()
    with open(file_path, 'rb') as f:
        while chunk := f.read(8192):
            sha1.update(chunk)
    return sha1.hexdigest()

def upload_file_to_s3(file_path: str, bucket: str, s3_key: str):
    try:
        s3_client.upload_file(file_path, bucket, s3_key,
                               ExtraArgs={'ContentType': 'text/html'})
        click.echo(f"File uploaded successfully to S3: {s3_key}")
    except Exception as e:
        raise RuntimeError(f"Failed to upload file to S3: {e}")

def create_presigned_url(bucket: str, object_key: str, expiration: int = 3600) -> str:
    try:
        response = s3_client.generate_presigned_url('get_object',
                                                    Params={'Bucket': bucket, 'Key': object_key},
                                                    ExpiresIn=expiration)
        return response
    except Exception as e:
        raise RuntimeError(f"Failed to create presigned URL: {e}")

if __name__ == "__main__":
    tinyhost()
