"""
Auth routes — register, login, logout, /me.

Uses Flask sessions (server-side) with bcrypt password hashing.
No JWT tokens — sessions are stored in a signed cookie.
"""

import logging
from functools import wraps

import bcrypt
import os
from flask import Blueprint, jsonify, request, session

from database import User, db

logger = logging.getLogger(__name__)

auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')


def login_required(f):
    """Decorator that returns 401 if no valid session exists."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Authentication required'}), 401
        return f(*args, **kwargs)
    return decorated


def _expected_invite_code():
    return os.environ.get('INVITE_CODE', '').strip()


@auth_bp.route('/register', methods=['POST'])
def register():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    invite_code = (data.get('invite_code') or '').strip()

    if not email or not password:
        return jsonify({'error': 'Email and password required'}), 400

    if len(password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters'}), 400

    # Invite code gate — only enforced when INVITE_CODE env var is set
    expected = _expected_invite_code()
    if expected and invite_code != expected:
        return jsonify({'error': 'Invalid invite code. Ask a teammate for the current code.'}), 403

    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'An account with that email already exists'}), 409

    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    user = User(email=email, password_hash=pw_hash)
    db.session.add(user)
    db.session.commit()

    session['user_id'] = user.id
    session.permanent = True
    return jsonify({'user': user.to_dict()}), 201


@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''

    if not email or not password:
        return jsonify({'error': 'Email and password required'}), 400

    user = User.query.filter_by(email=email).first()
    if not user or not bcrypt.checkpw(password.encode(), user.password_hash.encode()):
        return jsonify({'error': 'Invalid email or password'}), 401

    session['user_id'] = user.id
    session.permanent = True
    return jsonify({'user': user.to_dict()})


@auth_bp.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'message': 'Logged out'})


@auth_bp.route('/me', methods=['GET'])
@login_required
def me():
    user = User.query.get(session['user_id'])
    if not user:
        session.clear()
        return jsonify({'error': 'User not found'}), 404
    return jsonify({'user': user.to_dict()})
