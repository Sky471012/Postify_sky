from flask import Flask, render_template, redirect, session, url_for, request, flash, jsonify
from authlib.integrations.flask_client import OAuth
import os
from flask import request
from config import post_generation_template
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain_core.prompts import PromptTemplate
from langchain.chains import RetrievalQA
from langchain.vectorstores import FAISS
from werkzeug.utils import secure_filename
import tempfile
from langchain_community.tools import DuckDuckGoSearchRun
from langchain_groq import ChatGroq
from langchain_community.document_loaders import PyPDFLoader
import requests
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_core.tools import Tool
from langchain.agents import initialize_agent, AgentType
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")

# OAuth Config
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
LINKEDIN_CLIENT_ID = os.getenv("LINKEDIN_CLIENT_ID")
LINKEDIN_CLIENT_SECRET = os.getenv("LINKEDIN_CLIENT_SECRET")
LINKEDIN_REDIRECT_URI = "http://localhost:5000/linkedin/callback"
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
HF_TOKEN = os.getenv("HF_TOKEN")

embedding_model = HuggingFaceEmbeddings(model_name = 'all-MiniLM-L6-v2')

UPLOAD_FOLDER = "static/uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

@app.route("/upload", methods=["POST"])
def upload_pdf():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]

    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    # Save file temporarily
    temp_dir = tempfile.mkdtemp()
    file_path = os.path.join(temp_dir, file.filename)
    file.save(file_path)

    # Process PDF
    loader = PyPDFLoader(file_path)
    pages = loader.load_and_split()

    # Create vector store for retrieval
    db = FAISS.from_documents(pages, embedding_model)
    retriever = db.as_retriever()

    # Use RAG pipeline to extract key insights
    chain = RetrievalQA(llm=llm, retriever=retriever)
    summary = chain.run("Summarize this document in key points for a LinkedIn post.")

    return jsonify({"filename": file.filename, "summary": summary})

# Initialize Search Tool
search = DuckDuckGoSearchRun(name='Search')

# Initialize LLM
llm = ChatGroq(api_key=os.getenv("GROQ_API_KEY"), model="llama3-70b-8192")
prompt = PromptTemplate(
    input_variables=['context','user_input', 'writing_style'],
    template=post_generation_template,
    validate_template=True
)
tools = [search]
search_agent = initialize_agent(tools=tools, llm=llm, agent=AgentType.ZERO_SHOT_REACT_DESCRIPTION)
generation_chain = prompt | llm 

oauth = OAuth(app)
google = oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    api_base_url="https://www.googleapis.com/oauth2/v3/", 
    access_token_url="https://oauth2.googleapis.com/token",
    authorize_url="https://accounts.google.com/o/oauth2/auth",
    userinfo_endpoint="https://www.googleapis.com/oauth2/v3/userinfo",
    client_kwargs={"scope": "openid email profile"},
    jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
)

linkedin = oauth.register(
    name="linkedin",
    client_id=LINKEDIN_CLIENT_ID,
    client_secret=LINKEDIN_CLIENT_SECRET,
    access_token_url="https://www.linkedin.com/oauth/v2/accessToken",
    authorize_url="https://www.linkedin.com/oauth/v2/authorization",
    api_base_url="https://api.linkedin.com/v2/",
    userinfo_endpoint="https://api.linkedin.com/v2/me",
    client_kwargs={"scope": "openid profile email w_member_social"},
    grant_type="authorization_code", 
    server_metadata_url=None, 
)

def post_to_linkedin_api(access_token, post_content):
    user_id = session["user"]["id"]
    post_url = "https://api.linkedin.com/v2/ugcPosts"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0"
    }

    post_data = {
        "author": f"urn:li:person:{user_id}",
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {
                    "text": post_content
                },
                "shareMediaCategory": "NONE"
            }
        },
        "visibility": {
            "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
        }
    }

    response = requests.post(post_url, json=post_data, headers=headers)
    
    if response.status_code == 201:
        return True, "Post successfully published."
    else:
        return False, response.json()

@app.route("/post_to_linkedin", methods=["POST"])
def post_to_linkedin():
    access_token = session.get("linkedin_token")
    if not access_token:
        flash("User not authenticated with LinkedIn!", "danger")
        return redirect(url_for("dashboard"))

    post_content = request.form.get("final_post_content")

    success, response = post_to_linkedin_api(access_token, post_content)

    if success:
        flash("Post successfully published to LinkedIn!", "success")
        session.pop("generated_post") 
    else:
        flash(f"Failed to post on LinkedIn: {response}", "danger")

    return redirect(url_for("dashboard"))


@app.route('/')
def home():
    user = session.get("user")
    return render_template('index.html', user=user)

@app.route("/google/login")
def google_login():
    return google.authorize_redirect(url_for("google_callback", _external=True))

@app.route("/google/callback")
def google_callback():
    token = google.authorize_access_token()
    user_info = google.get("userinfo").json()
    session["user"] = user_info
    return redirect(url_for("dashboard", username=user_info["name"]))

@app.route("/linkedin/login")
def linkedin_login():
    return linkedin.authorize_redirect(
        url_for("linkedin_callback", _external=True),
        response_type="code" 
    )

@app.route("/linkedin/callback")
def linkedin_callback():

    auth_code = request.args.get("code")
    if not auth_code:
        return "Authorization code not found!", 400

    token_url = "https://www.linkedin.com/oauth/v2/accessToken"
    redirect_uri = url_for("linkedin_callback", _external=True)

    data = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": redirect_uri,
        "client_id": LINKEDIN_CLIENT_ID,
        "client_secret": LINKEDIN_CLIENT_SECRET,
    }

    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    response = requests.post(token_url, data=data, headers=headers)
    token_data = response.json()

    if "access_token" not in token_data:
        return f"Failed to get access token: {token_data}", 400

    access_token = token_data["access_token"]
    session["linkedin_token"] = access_token
    headers = {"Authorization": f"Bearer {access_token}"}
    user_info = requests.get("https://api.linkedin.com/v2/userinfo", headers=headers).json()

    user_id = user_info.get("sub")
    first_name = user_info.get("given_name", "Unknown")
    last_name = user_info.get("family_name", "User")
    profile_picture = user_info.get("picture", "../static/assets/images/logo/postify-logo-03-01.png")

    session["user"] = {
        "id": user_id,
        "name": f"{first_name} {last_name}",
        "profile_picture": profile_picture,
    }

    return redirect(url_for("dashboard"))

@app.route("/dashboard")
@app.route("/dashboard/<username>")
def dashboard(username=None):
    user = session.get("user")
    if not user:
        return redirect(url_for("login"))
    
    return render_template("dashboard.html", user=user)

@app.route("/login")
def login():
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect("/")

# ✅ Search Images (Pexels API)
@app.route("/search_images", methods=["POST"])
def search_images():
    data = request.get_json()
    query = data.get("query")

    if not query:
        return   ({"error": "Query is required"}), 400

    url = f"https://api.pexels.com/v1/search?query={query}&per_page=8"
    headers = {"Authorization": PEXELS_API_KEY}
    response = requests.get(url, headers=headers)

    return jsonify(response.json()) if response.status_code == 200 else jsonify({"error": "Failed to fetch images"}), 500

# ✅ Upload Image
@app.route("/upload_image", methods=["POST"])
def upload_image():
    if "image" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["image"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400

    filename = secure_filename(file.filename)
    file_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(file_path)

    return jsonify({"image_url": f"/{file_path}"})


@app.route("/generate_post", methods=["POST"])
def generate_post():
    user_input = request.form.get("user_input")
    writing_style = request.form.get("writing_style")
    web_responses = search_agent.invoke(user_input)
    if not user_input:
        return "No input provided", 400
    post_content = generation_chain.invoke({"context" : web_responses,"user_input": user_input, "writing_style": writing_style}).content
    session["generated_post"] = post_content

    return render_template("dashboard.html", user=session["user"], post=post_content)

if __name__ == '__main__':
    app.run(debug=True)