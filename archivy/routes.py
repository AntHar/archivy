import os
from datetime import datetime

import requests
import frontmatter
import pypandoc
from tinydb import Query, operations
from flask import render_template, flash, redirect, request, jsonify, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from flask_login import login_user, login_required, current_user, logout_user

from archivy.models import DataObj, User
from archivy.search import query_index
from archivy import data, app, forms
from archivy.extensions import get_db
from archivy.config import Config


@app.context_processor
def pass_defaults():
    dataobjs = data.get_items()
    SEP = os.path.sep
    # check windows parsing for js (https://github.com/Uzay-G/archivy/issues/115)
    if SEP == "\\":
        SEP += "\\"
    return dict(dataobjs=dataobjs, SEP=os.path.sep)


@app.before_request
def check_perms():
    allowed_path = request.path == "/login" or request.path.startswith("/static")
    if not current_user.is_authenticated and not allowed_path:
        return redirect(url_for("login", next=request.path))
    return


@app.route("/")
@app.route("/index")
def index():
    return render_template(
            "home.html",
            title="Home",
            search_enabled=Config.ELASTICSEARCH_ENABLED,
            )


# TODO: refactor two following methods
@app.route("/bookmarks/new", methods=["GET", "POST"])
def new_bookmark():
    form = forms.NewBookmarkForm()
    form.path.choices = [(pathname, pathname) for pathname in data.get_dirs()]
    if form.validate_on_submit():
        path = form.path.data if form.path.data != "not classified" else ""
        bookmark = DataObj(
            url=form.url.data,
            desc=form.desc.data,
            tags=form.tags.data.split(","),
            path=path,
            type="bookmarks")
        bookmark.process_bookmark_url()
        bookmark_id = bookmark.insert()
        if bookmark_id:
            flash("Bookmark Saved!")
            return redirect(f"/dataobj/{bookmark_id}")
    return render_template(
        "dataobjs/new.html",
        title="New Bookmark",
        form=form)


@app.route("/notes/new", methods=["GET", "POST"])
def new_note():
    form = forms.NewNoteForm()
    form.path.choices = [(pathname, pathname) for pathname in data.get_dirs()]
    if form.validate_on_submit():
        path = form.path.data if form.path.data != "not classified" else ""
        note = DataObj(
            title=form.title.data,
            desc=form.desc.data,
            tags=form.tags.data.split(","),
            path=path,
            type="note")
        note_id = note.insert()
        if note_id:
            flash("Note Saved!")
            return redirect(f"/dataobj/{note_id}")
    return render_template(
        "/dataobjs/new.html",
        title="New Note",
        form=form)


@app.route("/dataobj/<dataobj_id>")
def show_dataobj(dataobj_id):
    dataobj = data.get_item(dataobj_id)

    if not dataobj:
        flash("Data could not be found!")
        return redirect("/")

    if request.args.get("raw") == "1":
        return frontmatter.dumps(dataobj)

    extra_pandoc_args = ["--highlight-style="
                         + app.config['PANDOC_HIGHLIGHT_THEME'],
                         "--standalone"]

    content = pypandoc.convert_text(dataobj.content, 'html', format='md',
                                    extra_args=extra_pandoc_args)
    return render_template(
        "dataobjs/show.html",
        title=dataobj["title"],
        dataobj=dataobj,
        content=content,
        form=forms.DeleteDataForm())


@app.route("/dataobj/delete/<dataobj_id>", methods=["DELETE", "GET"])
def delete_data(dataobj_id):
    try:
        data.delete_item(dataobj_id)
    except BaseException:
        flash("Data could not be found!")
        return redirect("/")
    flash("Data deleted!")
    return redirect("/")


@app.route("/folders/new", methods=["POST"])
def create_folder():
    directory = request.json.get("paths")
    try:
        sanitized_name = data.create_dir(directory)
    except FileExistsError:
        return "Directory already exists", 401
    return sanitized_name, 200


@app.route("/folders/delete", methods=["DELETE"])
def delete_folder():
    directory = request.json.get("name")
    if directory == "":
        return "Cannot delete root dir", 401
    if data.delete_dir(directory):
        return "Successfully deleted", 200
    return "Not found", 404


@app.route("/search", methods=["GET"])
def search_elastic():
    query = request.args.get("query")
    search_results = query_index(Config.INDEX_NAME, query)
    return jsonify(search_results)


@app.route("/login", methods=["GET", "POST"])
def login():
    form = forms.UserForm()
    if form.validate_on_submit():
        db = get_db()
        user = db.search((Query().username == form.username.data) & (Query().type == "user"))

        if user and check_password_hash(user[0]["hashed_password"], form.password.data):
            user = User.from_db(user[0])
            login_user(user, remember=True)
            flash("Login successful!")

            next_url = request.args.get("next")
            return redirect(next_url or "/")

        flash("Invalid credentials")
        return redirect("/login")
    return render_template("users/form.html", form=form, title="Login")


@app.route("/logout", methods=["DELETE"])
@login_required
def logout():
    logout_user()
    flash("Logged out successfully")
    return redirect("/")


@app.route("/user/edit", methods=["GET", "POST"])
@login_required
def edit_user():
    form = forms.UserForm()
    if form.validate_on_submit():
        db = get_db()
        db.update(
            {
                "username": form.username.data,
                "hashed_password": generate_password_hash(form.password.data)
            },
            doc_ids=[current_user.id]
        )
        flash("Information saved!")
        return redirect("/")
    form.username.data = current_user.username
    return render_template("users/form.html", title="Edit Profile", form=form)


@app.route("/pocket", methods=["POST", "GET"])
def pocket_settings():
    db = get_db()
    form = forms.PocketForm()
    pocket = Query()
    if form.validate_on_submit():
        request_data = {
            "consumer_key": form.api_key.data,
            "redirect_uri": "http://localhost:5000/parse_pocket?new=1",
        }
        resp = requests.post(
            "https://getpocket.com/v3/oauth/request",
            json=request_data,
            headers={
                "X-Accept": "application/json",
                "Content-Type": "application/json"})
        new_data = {
            "type": "pocket_key",
            "consumer_key": form.api_key.data,
            "code": resp.json()["code"]}
        if db.search(pocket.type == "pocket_key"):
            db.update(new_data, pocket.type == "pocket_key")
        else:
            db.insert(new_data)
        flash("Settings Saved")
        return redirect(
            # FIXME: the redirect is forced to localhost:5000
            # but the server is started on 0.0.0.0
            # port 5000 might be on use by another resource
            # so add a check here
            f"https://getpocket.com/auth/authorize?"
            f"request_token={resp.json()['code']}"
            f"&redirect_uri=http://localhost:5000/"
            f"parse_pocket?new=1")

    return render_template(
        "pocket/new.html",
        title="Pocket Settings",
        form=form)


@app.route("/parse_pocket")
def parse_pocket():
    db = get_db()
    pocket = db.search(Query().type == "pocket_key")[0]
    if request.args.get("new") == "1":
        auth_data = {
            "consumer_key": pocket["consumer_key"],
            "code": pocket["code"]}
        resp = requests.post(
            "https://getpocket.com/v3/oauth/authorize",
            json=auth_data,
            headers={
                "X-Accept": "application/json",
                "Content-Type": "application/json"})
        db.update(
            operations.set(
                "access_token",
                resp.json()["access_token"]),
            Query().type == "pocket_key")
        flash(f"{resp.json()['username']} Signed in!")

    # update pocket dictionary
    pocket = db.search(Query().type == "pocket_key")[0]

    pocket_data = {
        "consumer_key": pocket["consumer_key"],
        "access_token": pocket["access_token"],
        "sort": "newest"}

    # get date of latest call to pocket api
    since = datetime(1970, 1, 1)
    for post in data.get_items(
            collections=["pocket_bookmark"],
            structured=False):
        date = datetime.strptime(post["date"].replace("-", "/"), "%x")
        since = max(date, since)

    since = datetime.timestamp(since)
    if since:
        pocket_data["since"] = since
    bookmarks = requests.post(
        "https://getpocket.com/v3/get",
        json=pocket_data).json()

    # api spec: https://getpocket.com/developer/docs/v3/retrieve
    for pocket_bookmark in bookmarks["list"].values():
        if int(pocket_bookmark["status"]) != 2:
            desc = pocket_bookmark["excerpt"] if int(
                pocket_bookmark["is_article"]) else None
            bookmark = DataObj(
                desc=desc,
                url=pocket_bookmark["resolved_url"],
                date=datetime.now(),
                type="pocket_bookmarks")
            bookmark.process_bookmark_url()
            bookmark.insert()

    return redirect("/")
