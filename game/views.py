"""Game views for the Checkora chess platform."""

import json
import time
import hashlib
import secrets
from smtplib import SMTPException
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.forms import AuthenticationForm, SetPasswordForm
from django.core.mail import BadHeaderError, send_mail
from django.contrib import messages
from django.db.models import F

from .forms import CustomUserCreationForm
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from django.db.models import Q


from .engine import ChessGame
from .models import GameResult

def landing(request):
    """Render the landing page introduction to Checkora."""
    return render(request, 'game/landing.html')

@login_required
@ensure_csrf_cookie
def index(request):
    """Render the board and initialise a new game in the session."""
    if 'game' not in request.session:
        game = ChessGame()
        request.session['game'] = game.to_dict()
    return render(request, 'game/board.html')

def record_game_result(request, mode, winner, reason, player_color='white'):
    """Save a completed game result to the database."""
    user = request.user if request.user.is_authenticated else None
    GameResult.objects.create(user=user, mode=mode, winner=winner, end_reason=reason, player_color=player_color)

@login_required
@require_POST
def make_move(request):
    """Validate and execute a chess move via the C++ engine."""
    try:
        data = json.loads(request.body)
        from_row = int(data['from_row'])
        from_col = int(data['from_col'])
        to_row = int(data['to_row'])
        to_col = int(data['to_col'])
        promotion_piece = data.get('promotion_piece', None)
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return JsonResponse(
            {'valid': False, 'message': 'Invalid request data.'},
            status=400,
        )

    game_data = request.session.get('game')
    game = ChessGame.from_dict(game_data) if game_data else ChessGame()

    success, message, captured, game_status = game.make_move(
        from_row, from_col, to_row, to_col, promotion_piece,
    )

    if success:
        request.session['game'] = game.to_dict()
        request.session.modified = True
        if game_status == 'checkmate':
            winner = 'black' if game.current_turn == 'white' else 'white'
            record_game_result(request, game.mode, winner, 'checkmate', game.player_color)
        elif game_status in ('stalemate', 'draw'):
            record_game_result(request, game.mode, 'draw', game.draw_reason or 'stalemate', game.player_color)

    return JsonResponse({
        'valid': success,
        'message': message,
        'captured': captured,
        'board': game.board,
        'current_turn': game.current_turn,
        'white_time': game.white_time,
        'black_time': game.black_time,
        'move_history': game.move_history,
        'captured_pieces': game.captured,
        'game_status': game_status,
        'draw_reason': game.draw_reason,
        'fen': game.generate_fen_key(),
        'pgn': game.generate_pgn(),
        'white_name': request.session.get('white_name', 'White'),
        'black_name': request.session.get('black_name', 'Black'),
    })

@login_required
@require_GET
def valid_moves(request):
    """Return every legal destination for a piece."""
    try:
        row = int(request.GET['row'])
        col = int(request.GET['col'])
    except (KeyError, ValueError, TypeError):
        return JsonResponse({'valid_moves': []}, status=400)

    if not (0 <= row < 8 and 0 <= col < 8):
        return JsonResponse({'valid_moves': []}, status=400)

    game_data = request.session.get('game')
    if not game_data:
        return JsonResponse({'valid_moves': []})

    game = ChessGame.from_dict(game_data)
    moves = game.get_valid_moves(row, col)
    return JsonResponse({'valid_moves': moves})

@login_required
@require_POST
def new_game(request):
    """Reset the game to the initial position with selected mode."""
    data = json.loads(request.body or '{}')
    mode = data.get('mode', 'pvp')
    difficulty = data.get('difficulty', 'medium')

    fen = data.get('fen')

    if mode not in ('pvp', 'ai'):
        mode = 'pvp'

    player_color = data.get('player_color', 'white')
    request.session['white_name'] = data.get('white_name', 'White')
    request.session['black_name'] = data.get('black_name', 'Black')

    def _clean_name(raw, fallback):
        name = (raw or '').strip()
        if not name or len(name) > 30:
            return fallback
        return name

    request.session['white_name'] = _clean_name(
        data.get('white_name'), 'White'
    )
    request.session['black_name'] = _clean_name(
        data.get('black_name'), 'Black'
    )

    request.session['difficulty'] = difficulty
    request.session['player_color'] = player_color

    fen = fen.strip() if isinstance(fen, str) else None
    if fen:
        try:
            game = ChessGame.from_fen(fen)
        except ValueError as exc:
            return JsonResponse(
                {'valid': False, 'message': f'Invalid FEN: {exc}'},
                status=400,
            )
    else:
        game = ChessGame()
    game.mode = mode
    game.player_color = player_color
    game.paused = False

    request.session['game'] = game.to_dict()
    request.session.modified = True

    return JsonResponse({
        'valid': True,
        'board': game.board,
        'current_turn': game.current_turn,
        'move_history': [],
        'captured_pieces': {'white': [], 'black': []},
        'mode': game.mode,
        'player_color': game.player_color,
        'white_name': request.session['white_name'],
        'black_name': request.session['black_name'],
        'difficulty': difficulty,
        'fen': game.generate_fen_key(),
        'pgn': game.generate_pgn(),
        'game_status': game.game_status,
        'draw_reason': game.draw_reason,
    })

@login_required
@require_GET
def check_promotion(request):
    """Return whether a planned move triggers pawn promotion."""
    try:
        from_row = int(request.GET['from_row'])
        from_col = int(request.GET['from_col'])
        to_row = int(request.GET['to_row'])
    except (KeyError, ValueError, TypeError):
        return JsonResponse({'is_promotion': False})

    if not (0 <= from_row < 8 and 0 <= from_col < 8 and 0 <= to_row < 8):
        return JsonResponse({'is_promotion': False})

    game_data = request.session.get('game')
    if not game_data:
        return JsonResponse({'is_promotion': False})

    is_promo = ChessGame.is_promotion_move(
        game_data['board'], from_row, from_col, to_row,
    )
    return JsonResponse({'is_promotion': is_promo})

@login_required
@require_GET
def get_state(request):
    """Return the full current game state without mutating pause state."""
    game_data = request.session.get('game')
    if not game_data:
        game = ChessGame()
    else:
        game = ChessGame.from_dict(game_data)
        elapsed = time.time() - game.last_ts
        if elapsed > 10 and not game.paused:
            game.paused = True
        else:
            game.update_clock()

    request.session['game'] = game.to_dict()
    request.session.modified = True

    return JsonResponse({
        'board': game.board,
        'current_turn': game.current_turn,
        'white_time': game.white_time,
        'black_time': game.black_time,
        'paused': game.paused,
        'move_history': game.move_history,
        'captured_pieces': game.captured,
        'mode': game.mode,
        'player_color': game.player_color,
        'difficulty': request.session.get('difficulty', 'medium'),
        'white_name': request.session.get('white_name', 'White'),
        'black_name': request.session.get('black_name', 'Black'),
        'fen': game.generate_fen_key(),
        'pgn': game.generate_pgn(),
        'game_status': game.game_status,
        'draw_reason': game.draw_reason,
    })

@login_required
@require_POST
def set_pause(request):
    """Toggle the game clock between paused and running."""
    game_data = request.session.get('game')
    if not game_data:
        return JsonResponse({'paused': False})

    data = json.loads(request.body or '{}')
    pause = data.get('pause', True)

    game = ChessGame.from_dict(game_data)
    if pause and not game.paused:
        game.update_clock()
    game.paused = pause
    game.last_ts = time.time()

    request.session['game'] = game.to_dict()
    request.session.modified = True

    return JsonResponse({
        'paused': game.paused,
        'white_time': game.white_time,
        'black_time': game.black_time,
    })

@login_required
@require_POST
def ai_move(request):
    """Let the engine compute and play the best move for the current side."""
    game_data = request.session.get('game')
    if not game_data:
        return JsonResponse({'valid': False, 'message': 'No active game.'}, status=400)

    game = ChessGame.from_dict(game_data)
    if game.mode != 'ai':
        return JsonResponse({'valid': False, 'message': 'Not in AI mode.'}, status=400)

    difficulty = request.session.get('difficulty', 'medium')
    depth_map = {'easy': 2, 'medium': 3, 'hard': 5}
    depth = depth_map.get(difficulty, 3)

    best = game.get_ai_move(depth=depth)
    if not best:
        return JsonResponse({
            'valid': False,
            'message': 'No legal moves available.',
            'board': game.board,
            'current_turn': game.current_turn,
        })

    success, message, captured, game_status = game.make_move(
        best['from_row'], best['from_col'],
        best['to_row'],   best['to_col'],
    )

    if success:
        request.session['game'] = game.to_dict()
        request.session.modified = True
        if game_status == 'checkmate':
            winner = 'black' if game.current_turn == 'white' else 'white'
            record_game_result(request, game.mode, winner, 'checkmate', game.player_color)
        elif game_status in ('stalemate', 'draw'):
            record_game_result(request, game.mode, 'draw', game.draw_reason or 'stalemate', game.player_color)

    return JsonResponse({
        'valid': success,
        'message': message,
        'captured': captured,
        'board': game.board,
        'current_turn': game.current_turn,
        'white_time': game.white_time,
        'black_time': game.black_time,
        'move_history': game.move_history,
        'captured_pieces': game.captured,
        'ai_move': best,
        'game_status': game_status,
        'draw_reason': game.draw_reason,
        'fen': game.generate_fen_key(),
        'pgn': game.generate_pgn(),
        'white_name': request.session.get('white_name', 'White'),
        'black_name': request.session.get('black_name', 'Black'),
    })

@login_required
@require_POST
def offer_draw(request):
    """Handle draw offers and agreements."""
    game_data = request.session.get('game')
    if not game_data:
        return JsonResponse({'success': False, 'message': 'No active game.'}, status=400)

    data = json.loads(request.body or '{}')
    action = data.get('action')

    if action == 'accept':
        game = ChessGame.from_dict(game_data)
        game.game_status = 'draw'
        game.draw_reason = 'agreement'
        request.session['game'] = game.to_dict()
        request.session.modified = True
        record_game_result(request, game.mode, 'draw', 'agreement', game.player_color)
        return JsonResponse({
            'success': True,
            'game_status': game.game_status,
            'draw_reason': game.draw_reason,
        })
    return JsonResponse({'success': True})

@login_required
@require_POST
def resign_game(request):
    """Handle a player resigning the game."""
    game_data = request.session.get('game')
    if not game_data:
        return JsonResponse({'valid': False, 'message': 'No active game.'}, status=400)

    game = ChessGame.from_dict(game_data)
    resigning_player = game.player_color if game.mode == 'ai' else game.current_turn
    winner = 'black' if resigning_player == 'white' else 'white'
    game.game_status = 'resignation'
    request.session['game'] = game.to_dict()
    request.session.modified = True
    record_game_result(request, game.mode, winner, 'resign', game.player_color)

    return JsonResponse({
        'valid': True,
        'message': f'{resigning_player.capitalize()} resigned.',
        'winner': winner,
        'game_status': 'resignation'
    })

def register_view(request):
    if request.user.is_authenticated:
        return redirect('index')
    if request.method == 'POST':
        form = CustomUserCreationForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.is_active = False
            user.save()
            otp = str(secrets.randbelow(900000) + 100000)
            request.session['registration_user_id'] = user.id
            otp_str = f"{otp}:{settings.SECRET_KEY}".encode()
            otp_hash = hashlib.sha256(otp_str).hexdigest()
            request.session['registration_otp_hash'] = otp_hash
            try:
                msg_plain = f'Your OTP for registration is: {otp}\n\nPlease enter this code to activate your account.'
                html_message = (
                    "<div style=\"font-family: 'Segoe UI', Arial, sans-serif; "
                    "background-color: #0f0f1a; color: #d0d0d0; padding: 40px "
                    "20px; text-align: center;\"><div style=\"background-"
                    "color: #16162a; border: 1px solid #252545; border-radius"
                    ": 12px; padding: 40px 30px; max-width: 450px; margin: 0 "
                    "auto; box-shadow: 0 10px 30px rgba(0,0,0,0.5);\">"
                    "<h1 style=\"color: #ffffff; margin-top: 0; margin-bottom"
                    ": 15px; font-size: 28px; letter-spacing: 2px;\">CHECK"
                    "<span style=\"color: #f0c040;\">ORA</span></h1>"
                    "<hr style=\"border: none; border-top: 1px solid #252545; "
                    "margin: 20px 0;\"><p style=\"color: #e0e0e0; font-size: "
                    "16px; line-height: 1.5; margin-bottom: 30px;\">Welcome "
                    "to the elite chess platform. To activate your account "
                    "and start playing, please use the verification code "
                    "below:</p><div style=\"margin: 35px 0;\"><span style=\""
                    "font-family: 'Consolas', monospace; font-size: 36px; "
                    "font-weight: bold; color: #f0c040; letter-spacing: 8px; "
                    "background: #0f0f1a; padding: 15px 25px; border-radius: "
                    "8px; border: 1px solid #3d3222; display: inline-block;"
                    "\">{otp}</span></div><p style=\"color: #8a8aaa; font-"
                    "size: 14px; margin-top: 30px;\">Enter this code on the "
                    "verification page to complete your registration.</p>"
                    "<p style=\"color: #5a5a7a; font-size: 12px; margin-top: "
                    "40px;\">If you didn't attempt to register on Checkora, "
                    "please safely ignore this email.</p></div></div>"
                ).format(otp=otp)
                send_mail('Your Checkora Verification Code', msg_plain, None, [user.email], fail_silently=False, html_message=html_message)
                return redirect('verify_otp')
            except (SMTPException, BadHeaderError, OSError):
                user.delete()
                request.session.pop('registration_user_id', None)
                request.session.pop('registration_otp_hash', None)
                messages.error(request, 'Failed to send OTP email. Please check your email address and try again.')
    else:
        form = CustomUserCreationForm()
    return render(request, 'game/register.html', {'form': form})

def verify_otp(request):
    if request.user.is_authenticated:
        return redirect('index')
    user_id = request.session.get('registration_user_id')
    stored_otp_hash = request.session.get('registration_otp_hash')
    if not user_id or not stored_otp_hash:
        messages.error(request, 'Session expired. Please register again.')
        return redirect('register')
    if request.method == 'POST':
        entered_otp = request.POST.get('otp', '').strip()
        entered_otp_hash = hashlib.sha256(f"{entered_otp}:{settings.SECRET_KEY}".encode()).hexdigest()
        if entered_otp_hash == stored_otp_hash:
            try:
                user = User.objects.get(id=user_id)
                user.is_active = True
                user.save()
                del request.session['registration_user_id']
                del request.session['registration_otp_hash']
                messages.success(request, 'Account verified successfully! Please log in to continue.')
                return redirect('login')
            except User.DoesNotExist:
                messages.error(request, 'User not found. Please register again.')
                return redirect('register')
        else:
            messages.error(request, 'Invalid OTP. Please try again.')
    return render(request, 'game/verify_otp.html')

def login_view(request):
    if request.method == 'POST':
        form = AuthenticationForm(data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            request.session.cycle_key()
            messages.success(request, f'Welcome back, {user.username}! You are now signed in.')
            return redirect('index')
    else:
        form = AuthenticationForm()
    return render(request, 'game/login.html', {'form': form})

@xframe_options_sameorigin
def rules(request):
    return render(request, 'game/rules.html')

@require_POST
def logout_view(request):
    logout(request)
    return redirect('index')

def forgot_password_view(request):
    if request.method == 'POST':
        email = request.POST.get('email', '').strip()
        try:
            user = User.objects.get(email=email)
            otp = str(secrets.randbelow(900000) + 100000)
            request.session['reset_user_id'] = user.id
            otp_str = f"{otp}:{settings.SECRET_KEY}".encode()
            otp_hash = hashlib.sha256(otp_str).hexdigest()
            request.session['reset_otp_hash'] = otp_hash
            msg_plain = f'Your OTP for password reset is: {otp}\n\nIf you did not request this, please ignore this email.'
            html_message = (
                "<div style=\"font-family: 'Segoe UI', Arial, sans-serif; "
                "background-color: #0f0f1a; color: #d0d0d0; padding: 40px "
                "20px; text-align: center;\"><div style=\"background-"
                "color: #16162a; border: 1px solid #252545; border-radius"
                ": 12px; padding: 40px 30px; max-width: 450px; margin: 0 "
                "auto; box-shadow: 0 10px 30px rgba(0,0,0,0.5);\">"
                "<h1 style=\"color: #ffffff; margin-top: 0; margin-bottom"
                ": 15px; font-size: 28px; letter-spacing: 2px;\">CHECK"
                "<span style=\"color: #f0c040;\">ORA</span></h1>"
                "<hr style=\"border: none; border-top: 1px solid #252545; "
                "margin: 20px 0;\"><p style=\"color: #e0e0e0; font-size: "
                "16px; line-height: 1.5; margin-bottom: 30px;\">Reset your "
                "password using the verification code below:</p><div style=\""
                "margin: 35px 0;\"><span style=\"font-family: 'Consolas', "
                "monospace; font-size: 36px; font-weight: bold; color: "
                "#f0c040; letter-spacing: 8px; background: #0f0f1a; padding: "
                "15px 25px; border-radius: 8px; border: 1px solid #3d3222; "
                "display: inline-block;\">{otp}</span></div><p style=\""
                "color: #8a8aaa; font-size: 14px; margin-top: 30px;\">"
                "Enter this code on the verification page to set a new password.</p>"
                "</div></div>"
            ).format(otp=otp)
            send_mail('Your Checkora Password Reset Code', msg_plain, None, [user.email], fail_silently=False, html_message=html_message)
            return redirect('verify_otp_password')
        except User.DoesNotExist:
            messages.error(request, 'No account found with that email address.')
        except Exception:
            messages.error(request, 'Failed to send reset email. Please try again.')
    return render(request, 'game/password_reset.html')

def verify_otp_password_view(request):
    user_id = request.session.get('reset_user_id')
    stored_otp_hash = request.session.get('reset_otp_hash')
    if not user_id or not stored_otp_hash:
        messages.error(request, 'Session expired. Please start over.')
        return redirect('forgot_password')
    if request.method == 'POST':
        entered_otp = request.POST.get('otp', '').strip()
        entered_otp_hash = hashlib.sha256(f"{entered_otp}:{settings.SECRET_KEY}".encode()).hexdigest()
        if entered_otp_hash == stored_otp_hash:
            request.session['reset_allowed'] = True
            return redirect('reset_password_confirm')
        else:
            messages.error(request, 'Invalid OTP. Please try again.')
    return render(request, 'game/verify_otp_password.html')

def reset_password_confirm_view(request):
    user_id = request.session.get('reset_user_id')
    reset_allowed = request.session.get('reset_allowed')
    if not user_id or not reset_allowed:
        messages.error(request, 'Unauthorized access. Please start over.')
        return redirect('forgot_password')
    try:
        user = User.objects.get(id=user_id)
        if request.method == 'POST':
            form = SetPasswordForm(user, request.POST)
            if form.is_valid():
                form.save()
                del request.session['reset_user_id']
                del request.session['reset_otp_hash']
                del request.session['reset_allowed']
                messages.success(request, 'Password reset successful! Please log in.')
                return redirect('login')
        else:
            form = SetPasswordForm(user)
        return render(request, 'game/password_reset_confirm.html', {'form': form})
    except User.DoesNotExist:
        messages.error(request, 'User not found.')
        return redirect('forgot_password')

@login_required
def stats_view(request):
    """Display game statistics."""
    user_results = GameResult.objects.filter(user=request.user).exclude(mode__in=['', None])
    recent = user_results.order_by('-played_at')[:20]
    ai_results = user_results.filter(mode='ai')
    user_ai_wins = ai_results.filter(winner=F('player_color')).count()

    ai_wins = ai_results.exclude(winner=F('player_color')).exclude(winner='draw').count()

    # If winner != player_color and not a draw, the AI won
    ai_wins = ai_results.filter(
        Q(winner='white', player_color='black') | Q(winner='black', player_color='white')
    ).count()

    ai_draws = ai_results.filter(winner='draw').count()
    ai_total = ai_results.count()
    return render(request, 'game/stats.html', {
        'recent': recent,
        'ai_total': ai_total,
        'user_ai_wins': user_ai_wins,
        'ai_wins': ai_wins,
        'ai_draws': ai_draws,
    })
