import os
import requests
import logging
import time
import sys
import re
import base64
from flask import Flask, request, render_template, jsonify
from dotenv import load_dotenv
from git import Repo

# This is a separate Flask app for the gallery editor
# Run it separately: python gallery_editor.py
# Access at http://localhost:5001/
# It provides a drag-and-drop editor for index.html in the local galeri repo
# Also, has a button to auto-update by scanning GitHub repos starting with 'gallery-'

app = Flask(__name__, template_folder='editor_templates')

# Setup logging to file, but with graceful handling
logging.basicConfig(filename='editor.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv()

GITHUB_USERNAME = os.getenv('GITHUB_USERNAME')
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GALLERY_REPO_NAME = 'galeri'
LOCAL_REPO_BASE = os.getenv('LOCAL_REPO_BASE', 'repos')

gallery_path = os.path.join(LOCAL_REPO_BASE, GALLERY_REPO_NAME)
index_path = os.path.join(gallery_path, 'index.html')

# Ensure gallery repo is cloned
if not os.path.exists(gallery_path):
    gallery_clone_url = f'https://{GITHUB_USERNAME}:{GITHUB_TOKEN}@github.com/{GITHUB_USERNAME}/{GALLERY_REPO_NAME}.git'
    Repo.clone_from(gallery_clone_url, gallery_path)
    logging.info(f'Gallery repo cloned to {gallery_path}')

def clean_log():
    logger = logging.getLogger()
    for handler in logger.handlers[:]:
        if isinstance(handler, logging.FileHandler):
            handler.close()
            logger.removeHandler(handler)
    if os.path.exists('editor.log'):
        retries = 5
        for _ in range(retries):
            try:
                os.remove('editor.log')
                break
            except OSError as e:
                if getattr(e, 'winerror', None) != 32:
                    logging.warning(f'Non-retryable error deleting editor.log: {str(e)}')
                    return  # Exit without raising
                time.sleep(0.2)  # Increased sleep time for better chance of release
        else:
            logging.warning('Failed to delete editor.log after retries; skipping cleanup.')
            return  # Don't raise; just warn
    # Reconfigure logging
    logging.basicConfig(filename='editor.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    logging.info('Cleaned editor.log')

def update_github_file(content, file_path='index.html'):
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github+json'
    }
    file_url = f'https://api.github.com/repos/{GITHUB_USERNAME}/{GALLERY_REPO_NAME}/contents/{file_path}'
    # Get current SHA
    response = requests.get(file_url, headers=headers)
    sha = None
    if response.status_code == 200:
        sha = response.json()['sha']
    elif response.status_code != 404:
        raise Exception(f'Failed to get file info: {response.json().get("message")}')
    
    # Prepare data
    data = {
        'message': f'Updated {file_path}',
        'content': base64.b64encode(content.encode('utf-8')).decode('utf-8')
    }
    if sha:
        data['sha'] = sha
    
    # Update or create
    response = requests.put(file_url, headers=headers, json=data)
    if response.status_code not in [200, 201]:
        raise Exception(f'Failed to update GitHub file: {response.json().get("message")}')

def update_github_readme(slides_html):
    # Build beautiful README content with Markdown formatting
    # Convert <ul> items to Markdown list, make it visually appealing
    md_list = ''
    li_items = re.findall(r'<li>(.*?)</li>', slides_html, re.DOTALL)
    for item in li_items:
        # Extract link, name, description, thumbnail
        match = re.search(r'<a href="(.*?)">(.*?)</a> - (.*?)(<img.*?>)?', item.strip())
        if match:
            link, name, desc, thumb = match.groups()
            thumb_md = f'![Thumbnail]({link}thumbnail.jpg)' if thumb else ''
            md_list += f'- [**{name}**]({link}) - {desc}  \n  {thumb_md}\n\n'
    
    readme_content = f"""
# Slide Gallery 🎨

Welcome to the Slide Gallery! This repository hosts a collection of interactive slides. 

View the live gallery at [https://metinciris.github.io/galeri/](https://metinciris.github.io/galeri/).

## Slides Overview

{md_list}

## How to Use
- Browse the slides via the links above.
- Each slide is hosted in its own repository (named `gallery-*`).
- This README is auto-updated whenever the gallery changes.

Updated automatically on {time.strftime('%Y-%m-%d %H:%M:%S')}.
    """
    update_github_file(readme_content, file_path='README.md')
    logging.info('Updated README.md on GitHub')

@app.route('/')
def editor():
    # Load current index.html content
    if os.path.exists(index_path):
        with open(index_path, 'r', encoding='utf-8') as f:
            content = f.read()
    else:
        content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Slide Gallery</title>
</head>
<body>
    <h1>Slide Gallery</h1>
    <ul id="sortable">
    </ul>
</body>
</html>
        """
        with open(index_path, 'w', encoding='utf-8') as f:
            f.write(content)
    
    # Extract slides list for editing
    start = content.find('<ul id="sortable">')
    end = content.find('</ul>')
    if start != -1 and end != -1:
        list_content = content[start+18:end]
        li_matches = re.findall(r'<li>(.*?)</li>', list_content, re.DOTALL)
        slides_html = '\n        '.join([f'<li><textarea>{m.strip()}</textarea></li>' for m in li_matches])
    else:
        slides_html = ''
    
    return render_template('editor.html', slides_html=slides_html)

@app.route('/save', methods=['POST'])
def save():
    try:
        slides_html = request.form.get('slides_html')
        # Rebuild full HTML
        gallery_html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Slide Gallery</title>
</head>
<body>
    <h1>Slide Gallery</h1>
    <ul id="sortable">
        {slides_html}
    </ul>
</body>
</html>
        """
        # Save locally (optional, since PC local isn't critical)
        with open(index_path, 'w', encoding='utf-8') as f:
            f.write(gallery_html)
        
        # Update GitHub index.html
        update_github_file(gallery_html, file_path='index.html')
        
        # Update README
        update_github_readme(slides_html)
        
        logging.info('Gallery index.html and README.md saved on GitHub')
        
        try:
            clean_log()
        except Exception as e:
            logging.warning(f'Log cleanup failed: {str(e)}')
        
        return jsonify({'success': True, 'message': 'Gallery saved on GitHub (index.html and README.md updated).', 'html_content': gallery_html})
    except Exception as e:
        logging.error(f'Error saving gallery: {str(e)}')
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/auto_update', methods=['POST'])
def auto_update():
    try:
        # Fetch all repos starting with 'gallery-'
        headers = {
            'Authorization': f'token {GITHUB_TOKEN}',
            'Accept': 'application/vnd.github+json'
        }
        repos_url = f'https://api.github.com/users/{GITHUB_USERNAME}/repos?per_page=100'
        response = requests.get(repos_url, headers=headers)
        if response.status_code != 200:
            raise Exception(f'Failed to fetch repos: {response.json().get("message")}')
        
        repos = [repo['name'] for repo in response.json() if repo['name'].startswith('gallery-')]
        logging.info(f'Found {len(repos)} gallery repos')

        # Load current slides (from local, but could fetch from GitHub if local not important)
        with open(index_path, 'r', encoding='utf-8') as f:
            content = f.read()
        start = content.find('<ul id="sortable">')
        end = content.find('</ul>')
        existing_links = set()
        if start != -1 and end != -1:
            list_content = content[start+18:end]
            # Find all <li> items
            li_items = re.findall(r'<li>.*?</li>', list_content, re.DOTALL)
            # Extract repo_name from each <li>
            regex = r'<a href="https://{0}\.github\.io/([^/]+)/">'.format(re.escape(GITHUB_USERNAME))
            for li in li_items:
                match = re.search(regex, li)
                if match:
                    existing_links.add(match.group(1))

        # For each repo, fetch README.md to get details if exists
        new_slides = []
        for repo_name in repos:
            pages_link = f'https://{GITHUB_USERNAME}.github.io/{repo_name}/'
            if repo_name in existing_links:
                continue  # Already exists, skip
            
            # Fetch README.md content
            readme_url = f'https://api.github.com/repos/{GITHUB_USERNAME}/{repo_name}/contents/README.md'
            readme_response = requests.get(readme_url, headers=headers)
            time.sleep(1)  # Rate limit buffer
            if readme_response.status_code != 200:
                continue  # Skip if no README
            
            readme_data = readme_response.json()
            readme_content = base64.b64decode(readme_data['content']).decode('utf-8')
            
            if not readme_content.strip():
                continue  # Skip if README empty
            
            # Check for thumbnail existence (optional, to avoid broken images)
            thumb_url = f'{pages_link}thumbnail.jpg'
            thumb_response = requests.head(thumb_url)
            thumbnail = f' <img src="{thumb_url}" alt="Thumbnail" width="100">' if thumb_response.status_code == 200 else ''
            
            # Parse README for slide_name, description
            lines = readme_content.split('\n')
            slide_name = lines[0].strip('# ').strip() if lines and lines[0].startswith('#') else repo_name
            description = []
            for line in lines[1:]:
                if line.strip() and not line.startswith('View the slide') and not line.startswith('![Thumbnail]'):
                    description.append(line.strip())
            
            description_text = ' '.join(description) if description else 'No description'
            
            entry = f'<li><a href="{pages_link}">{slide_name}</a> - {description_text}{thumbnail}</li>'
            new_slides.append(entry)

        if new_slides:
            # Append new slides
            current_list_content = content[start+18:end].rstrip() if start != -1 else ''
            updated_list = current_list_content + '\n        ' + '\n        '.join(new_slides) + '\n    '
            updated_html = content[:start+18] + updated_list + content[end:]
            # Save locally
            with open(index_path, 'w', encoding='utf-8') as f:
                f.write(updated_html)
            
            # Update GitHub index.html
            update_github_file(updated_html, file_path='index.html')
            
            # Update README with the new slides_html
            update_github_readme(updated_list)
            
            logging.info(f'Added {len(new_slides)} new slides to local and GitHub index.html and README.md')
            
            try:
                clean_log()
            except Exception as e:
                logging.warning(f'Log cleanup failed: {str(e)}')
            
            return jsonify({'success': True, 'message': f'Added {len(new_slides)} new slides to GitHub (index.html and README.md updated). Reload the page to see them.', 'html_content': updated_html})
        else:
            return jsonify({'success': True, 'message': 'No new slides found or all repos are empty.', 'html_content': content})
    
    except Exception as e:
        logging.error(f'Error auto-updating gallery: {str(e)}')
        return jsonify({'success': False, 'message': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5001)  # Run on port 5001 to avoid conflict with main app
