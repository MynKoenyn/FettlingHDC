# Replit MD

## Overview

This is a **Fettling** application — an inventory/supply chain data entry system for tracking suppliers, products, and daily quantity entries. The app uses a Python Flask backend serving server-rendered HTML templates (Jinja2) with a PostgreSQL database. The project was scaffolded from a Replit full-stack template (React + Express + Drizzle), but the actual application logic runs entirely through Python/Flask. The Node.js entry point (`server/index.ts`) simply spawns the Python seed script and then the Flask app.

## User Preferences

Preferred communication style: Simple, everyday language.

## System Architecture

### Runtime Architecture
- **Primary backend**: Python Flask (`app.py`) serving HTML templates with Jinja2
- **Entry point**: `server/index.ts` acts as a launcher — it runs `seed.py` first (to populate initial data), then starts `app.py` via `child_process.spawn`
- **Frontend**: Server-rendered HTML templates in `templates/` directory, styled with plain CSS in `static/style.css`. No React frontend is actively used — the `client/` directory contains boilerplate React/shadcn components from the original template but they are not wired up to actual app functionality

### Database
- **PostgreSQL** via `psycopg2` (Python driver) for the Flask app
- **Drizzle ORM** schema exists in `shared/schema.ts` (only defines a basic users table) but is not used by the Flask app
- Database tables are created in `seed.py` and queried directly with raw SQL in `app.py`
- Key tables: `users`, `suppliers`, `products`, `fettling_entries` (daily quantity entries linking products/suppliers to dates)

### Authentication
- Simple session-based auth using Flask sessions
- Plain text password comparison (no hashing) — noted as intentionally simplified
- Default seed user: `admin` / `password`

### Pages / Routes (Flask)
- `/login` — Login form
- `/dashboard` — Shows recent activity (daily entry summaries)
- `/entry` — Daily data entry form (up to 50 line items per submission) with supplier → product filtering
- `/suppliers` — CRUD for suppliers
- `/products` — CRUD for products (linked to suppliers)
- `/api/products/<supplier_id>` — JSON API endpoint for filtering products by supplier (used by JavaScript in the entry form)

### Key Design Decisions
1. **Python Flask over the React/Express stack**: The app was built as a traditional server-rendered web app. The React frontend (`client/`) and Express server (`server/routes.ts`, `server/storage.ts`) are leftover scaffolding and not actively used.
2. **Raw SQL over ORM**: The Flask app uses `psycopg2` directly with parameterized queries rather than an ORM, keeping things simple.
3. **Node.js as process launcher**: `server/index.ts` exists only to satisfy Replit's `npm run dev` workflow — it spawns Python processes.

### Build & Dev
- `npm run dev` triggers `tsx server/index.ts` which runs `seed.py` then `app.py`
- Flask runs on port 5000 by default
- The Vite/React build pipeline exists but is not used for the actual application

## External Dependencies

### Database
- **PostgreSQL** — Required. Connection via `DATABASE_URL` environment variable. Used by both `psycopg2` (Python) and Drizzle config (TypeScript, though not actively queried from TS)

### Python Dependencies
- `flask` — Web framework
- `psycopg2` — PostgreSQL adapter
- `werkzeug` — Referenced in seed.py import but not actively used for password hashing

### Node.js Dependencies (from template, mostly unused)
- `express`, `drizzle-orm`, `drizzle-zod` — Server-side scaffolding (not actively used)
- `react`, `@tanstack/react-query`, `wouter` — Client-side scaffolding (not actively used)
- `shadcn/ui` components (Radix primitives) — Full component library installed but not connected to the Flask app
- `vite` — Build tool for the React frontend (not used in production flow)

### Environment Variables
- `DATABASE_URL` — PostgreSQL connection string (required)
- `SESSION_SECRET` — Flask session secret key (optional, has fallback default)