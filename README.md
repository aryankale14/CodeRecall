# 🌌 AetherReview — AI-Powered Codebase Intelligence

AetherReview is a premium, responsive repository analysis platform. It uses a custom Map-Reduce agentic workflow to clone, index, audit, and explain entire GitHub codebases. Users can view auto-generated global architecture reports, run comprehensive security audits, inspect file-by-file breakdowns, and chat directly with their codebase using a semantic RAG assistant.

---

## ✨ Features
- **Map-Reduce AI Analysis:** Analyzes individual files in parallel (Map Phase) and synthesizes a master explanation and security audit (Reduce Phase).
- **Semantic Codebase Q&A:** Chat with your repository using vector embeddings (Gemini Embeddings + PostgreSQL `pgvector`).
- **Interactive Code Viewer:** Explore file contents directly in the dashboard alongside their AI summaries and component details.
- **Platform Analytics:** Real-time metrics showing total repositories scanned, active users, and scanned file sizes.
- **Fully Responsive & Theme-Aware:** Sleek, glassmorphic UI with persistent Dark/Light mode and full mobile layout optimization.
- **Firebase Auth (Desktop & Mobile):** Unified Google Login with mobile redirection support to bypass browser popup blockers.

---

## 🛠️ Tech Stack
* **Frontend:** Next.js (App Router, TypeScript, Vanilla CSS, Lucide Icons)
* **Backend:** FastAPI (Python, SQLAlchemy, Pydantic)
* **Database:** PostgreSQL (with `pgvector` extension, hosted on Supabase)
* **Authentication:** Firebase Auth
* **AI Engine:** Google Gemini API (`gemini-1.5-flash` & `gemini-1.5-pro`)

---

## 🏗️ Architecture Overview

```mermaid
graph TD
    User([User]) -->|Submits Git URL| Frontend[Next.js Frontend]
    Frontend -->|Authorized POST| Backend[FastAPI Backend]
    Backend -->|Clone & Parse| Git[Git Clone / PyGithub]
    Git -->|Save Files| DB[(Supabase PostgreSQL + pgvector)]
    
    Backend -->|Background Task Chaining| MapPhase[Worker Agents: Summarize & Vectorize Files]
    MapPhase -->|Update DB| DB
    MapPhase -->|Trigger| ReducePhase[Master Agent: Create Global & Security Report]
    ReducePhase -->|Save Report| DB
    
    User -->|Ask Question| Chat[RAG Chat Controller]
    Chat -->|Compute Query Embedding| Gemini[Gemini Embedding API]
    Gemini -->|Vector Similarity Search| DB
    DB -->|Context Retrieval| Chat
    Chat -->|Generate Answer| User
```

---

## 🚀 Local Development Setup

### 1. Prerequisites
- Python 3.10+ installed
- Node.js 18+ installed
- A PostgreSQL database (e.g., Supabase) with the `pgvector` extension enabled:
  ```sql
  CREATE EXTENSION IF NOT EXISTS vector;
  ```

### 2. Backend Setup
Navigate into the backend directory:
```bash
cd backend
```

Create a virtual environment and activate it:
```bash
# Windows
python -m venv venv
venv\Scripts\activate

# Unix/Mac
python3 -m venv venv
source venv/bin/activate
```

Install dependencies:
```bash
pip install -r requirements.txt
```

Create a `.env` file in the `backend/` directory:
```env
DATABASE_URL=postgresql://postgres.your-supabase-db:password@aws-0-us-east-1.pooler.supabase.com:6543/postgres
GEMINI_API_KEY_MAP=your_gemini_api_key_1
GEMINI_API_KEY_REDUCE=your_gemini_api_key_2
GEMINI_API_KEY_RAG=your_gemini_api_key_3
FIREBASE_PROJECT_ID=your-firebase-project-id
```
*(Tip: You can use the same Gemini API key for all three keys, or split them to distribute rate limit allowances).*

Start the FastAPI server on port 9999:
```bash
uvicorn app.main:app --port 9999 --reload
```

---

### 3. Frontend Setup
Navigate into the frontend directory:
```bash
cd ../frontend
```

Install dependencies:
```bash
npm install
```

Create a `.env.local` file in the `frontend/` directory:
```env
# Backend API Location
NEXT_PUBLIC_BACKEND_URL=http://localhost:9999

# Firebase Client configuration (Leave blank/remove to use local Developer Mock Mode)
NEXT_PUBLIC_FIREBASE_API_KEY=AIzaSy...
NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=your-project.firebaseapp.com
NEXT_PUBLIC_FIREBASE_PROJECT_ID=your-project
NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET=your-project.appspot.com
NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID=000000000000
NEXT_PUBLIC_FIREBASE_APP_ID=1:000000000000:web:000000000000
```

Start the Next.js development server:
```bash
npm run dev
```
Open [http://localhost:3000](http://localhost:3000) to view the application.

---

## 🌐 Production Deployment Guide

AetherReview has been structured to make deployment on cloud providers easy.

### 1. Frontend Deployment (Netlify)
The frontend uses the pre-configured [netlify.toml](file:///netlify.toml) file in the root directory to handle Next.js subdirectory builds.

To deploy:
1. Connect your GitHub repository to **Netlify**.
2. Netlify will auto-detect the root `netlify.toml` and apply the settings:
   - **Base directory:** `frontend`
   - **Build command:** `npm run build`
   - **Publish directory:** `.next`
3. Add the following **Environment Variables** in the Netlify site console:
   - `NEXT_PUBLIC_BACKEND_URL`: The URL where your backend API is deployed (e.g. `https://your-backend.koyeb.app`).
   - All your frontend Firebase environment variables (`NEXT_PUBLIC_FIREBASE_*`).

---

### 2. Backend Deployment (Hugging Face Spaces - Free Docker Host)
Since the backend processes long-running AI operations and clones repositories in the background, serverless hosts (like Vercel or Netlify functions) will timeout or freeze. Instead, you can host it completely for free on **Hugging Face Spaces** using their Docker SDK (no credit card required).

A pre-configured `Dockerfile` has been provided at the root of the project to support this.

#### Deployment Steps:
1. Sign up or log in to [Hugging Face](https://huggingface.co/).
2. Create a new Space:
   - **Space Name:** `aether-backend` (or any name you like)
   - **License:** `mit`
   - **SDK:** Select **Docker** (choose the **Blank** template).
   - **Visibility:** Public (your code will be public, but your environment variables and database will remain completely secure).
3. Connect your GitHub repository:
   - You can link your GitHub repository to the Hugging Face Space for auto-deploys, or push manually to the HF Git remote.
4. Go to the **Settings** tab in your Space dashboard:
   - Scroll down to the **Variables and secrets** section.
   - Add the following as **Secrets** (do not add them as regular Variables, as Secrets are hidden):
     - `DATABASE_URL` (Supabase connection string)
     - `GEMINI_API_KEY_MAP`
     - `GEMINI_API_KEY_REDUCE`
     - `GEMINI_API_KEY_RAG`
     - `FIREBASE_PROJECT_ID`
5. Hugging Face will automatically detect the root `Dockerfile`, build your container, and start the FastAPI server.
6. Once the space status changes to **Running**, your backend endpoint will be available at:
   `https://<your-username>-<your-space-name>.hf.space` (e.g. `https://aryankale14-aether-backend.hf.space`).
7. Use this URL (with a trailing slash) as the `NEXT_PUBLIC_BACKEND_URL` environment variable when deploying your frontend on Netlify.

---

## 🔒 Firebase Security & Domain Whitelisting

If using production Firebase Auth:
1. Go to your **Firebase Console** > **Authentication** > **Settings** > **Authorized Domains**.
2. Click **Add Domain** and add your Netlify custom subdomain (e.g., `your-site.netlify.app`).
3. For local mobile testing on the same Wi-Fi network, add your local laptop IP address (e.g., `10.165.121.76`).
