#!/usr/bin/env python3
"""
This program:
  - Interfaces with Stockfish 17 (with NNUE enabled) via UCI.
  - Uses an openings dictionary (e.g. "Catalan") to play an opening line.
  - After the opening line is reached, it queries the Lichess Masters database 
    (and supplements with engine analysis) to obtain candidate moves.
  - For each candidate move, it follows the engine’s principal variation (up to a specified depth)
    while analyzing each move for “criticality” (i.e. moves that are effectively forced).
  - SVG images are generated for key positions.
  - A textual explanation of the main ideas/plans is printed.
  - A command–line spinner (loading animation) gives user feedback during analysis.
  
Before running:
  - Install dependencies: pip install python-chess requests
  - Download Stockfish 17 (with NNUE enabled) and update `engine_path` accordingly.
"""

import chess
import chess.engine
import chess.svg
import requests
import threading
import itertools
import sys
import time

# --- Dictionary of openings (expand as needed) ---
OPENING_DICT = {
    "catalan": ["d4", "Nf6", "c4", "e6", "Nf3", "d5", "g3", "Be7", "Bg2", "O-O", "0-0", "dxc4", "Qc2", "a6", "a4", "Nc6", "Qxc4", "Qd5"],
    # Add more openings here…
}


def get_opening_moves(opening_name):
    """Return the move sequence for the given opening name (case–insensitive)."""
    key = opening_name.lower()
    if key in OPENING_DICT:
        return OPENING_DICT[key]
    else:
        print("Opening not found. Try one of:", ", ".join(OPENING_DICT.keys()))
        return None


def get_fen_from_moves(moves_list):
    """Play the given moves (in SAN) on a new board and return the resulting FEN."""
    board = chess.Board()
    try:
        for move in moves_list:
            board.push_san(move)
    except Exception as e:
        print(f"Error processing moves {moves_list}: {e}")
        return None
    return board.fen()


def query_lichess_openings(fen):
    """
    Query the Lichess Masters opening explorer API for a given FEN.
    Documentation: https://explorer.lichess.ovh/
    """
    url = f"https://explorer.lichess.ovh/masters?fen={fen}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        return data
    except Exception as e:
        print("Error querying Lichess API:", e)
        return None


def get_top_moves_from_lichess(data, top_n=3):
    """
    From the JSON data returned by the Lichess API,
    return the top_n moves (sorted by total game count).
    """
    if "moves" not in data:
        return []
    moves = data["moves"]
    moves_sorted = sorted(
        moves,
        key=lambda x: x.get("white", 0) + x.get("draws", 0) + x.get("black", 0),
        reverse=True,
    )
    return moves_sorted[:top_n]


def evaluate_move_criticality(board, move, engine, threshold=50, analysis_time=0.5):
    """
    Given a board position (before the move is played) and a candidate move,
    evaluate the move using the engine versus all legal alternatives.
    
    A move is considered “critical” (or an “only move”) if the engine’s evaluation
    drops by at least `threshold` centipawns when choosing any alternative.
    
    Returns a tuple: (is_critical, score_diff)
      - is_critical: True if the move is critical
      - score_diff: the difference in centipawns between the candidate move and its best alternative
    """
    # Evaluate the candidate move:
    board.push(move)
    try:
        info = engine.analyse(board, chess.engine.Limit(time=analysis_time))
    except Exception:
        info = {"score": chess.engine.PovScore(chess.engine.Cp(0), chess.WHITE)}
    score_obj = info["score"].white()  # Evaluation from White’s perspective
    best_score = score_obj.score(mate_score=10000)  # in centipawns
    board.pop()

    # Evaluate every legal alternative move in the current position:
    alternative_scores = []
    for alt in board.legal_moves:
        board.push(alt)
        try:
            info_alt = engine.analyse(board, chess.engine.Limit(time=analysis_time))
        except Exception:
            info_alt = {"score": chess.engine.PovScore(chess.engine.Cp(0), chess.WHITE)}
        alt_score_obj = info_alt["score"].white()
        alt_score = alt_score_obj.score(mate_score=10000)
        alternative_scores.append((alt, alt_score))
        board.pop()

    # Exclude the candidate move from alternatives:
    alternative_scores = [(m, s) for (m, s) in alternative_scores if m != move]
    if not alternative_scores:
        return False, 0

    best_alternative = max(s for m, s in alternative_scores)
    score_diff = best_score - best_alternative

    is_critical = score_diff >= threshold
    return is_critical, score_diff


# --- Spinner class for loading animation ---
class Spinner:
    def __init__(self, message="Loading "):
        self.message = message
        self.done = False
        self.thread = threading.Thread(target=self.spin)

    def spin(self):
        for c in itertools.cycle(['|', '/', '-', '\\']):
            if self.done:
                break
            sys.stdout.write('\r' + self.message + c)
            sys.stdout.flush()
            time.sleep(0.1)
        sys.stdout.write('\r')

    def start(self):
        self.thread.start()

    def stop(self):
        self.done = True
        self.thread.join()


def get_variation_candidates(board, engine, top_moves_count=3, analysis_time=0.5):
    """
    From the given board position, get candidate moves.
    First, attempt to use the Lichess Masters database; if fewer than
    top_moves_count moves are returned, use engine analysis to supplement.
    Returns a list of chess.Move objects.
    """
    candidate_moves = []
    fen = board.fen()
    data = query_lichess_openings(fen)
    if data and "moves" in data:
        moves = data["moves"]
        sorted_moves = sorted(
            moves,
            key=lambda x: x.get("white", 0) + x.get("draws", 0) + x.get("black", 0),
            reverse=True,
        )
        for move_data in sorted_moves[:top_moves_count]:
            san = move_data.get("san")
            if san:
                try:
                    move = board.parse_san(san)
                    candidate_moves.append(move)
                except Exception:
                    continue
    # If not enough candidates, ask the engine.
    if len(candidate_moves) < top_moves_count:
        try:
            infos = engine.analyse(board, chess.engine.Limit(time=analysis_time), multipv=top_moves_count)
            for info in infos:
                pv = info.get("pv")
                if pv and pv[0] not in candidate_moves:
                    candidate_moves.append(pv[0])
        except Exception:
            pass
    return candidate_moves[:top_moves_count]


def generate_plan_explanation(board, engine, analysis_time=0.5):
    """
    Generate a simple textual explanation of the main ideas/plans for both sides.
    This example uses a basic evaluation heuristic.
    """
    try:
        info = engine.analyse(board, chess.engine.Limit(time=analysis_time))
        score_obj = info["score"].white()
        score = score_obj.score(mate_score=10000)
    except Exception:
        score = None

    if score is None:
        explanation = ("The position is unclear. Both sides should focus on completing development "
                       "and coordinating their pieces.")
    elif score > 100:
        explanation = (
            "White appears to hold an advantage. White’s plan may include expanding central control, "
            "activating pieces for a kingside or central attack, and exploiting weaknesses. "
            "Black should focus on completing development, challenging White’s center, and seeking counterplay "
            "(for example, through queenside expansion or piece exchanges)."
        )
    elif score < -100:
        explanation = (
            "Black appears to have an advantage. Black’s plan may include counterattacking, "
            "exploiting weaknesses in White’s structure, and consolidating the position. "
            "White should look to complete development quickly and create tactical opportunities to restore balance."
        )
    else:
        explanation = (
            "The position is relatively balanced. Both sides should complete development, "
            "fight for central control, and prepare for potential transitions into a favorable middlegame."
        )
    return explanation


def generate_position_svg(board, filename="board.svg"):
    """
    Generate an SVG image of the current board position and save it to a file.
    """
    svg_data = chess.svg.board(board=board, size=350)
    try:
        with open(filename, "w") as f:
            f.write(svg_data)
        print(f"Saved board image to {filename}")
    except Exception as e:
        print("Error saving SVG:", e)


def analyze_variation(start_board, candidate_move, engine, variation_depth=5, analysis_time=0.5, threshold=50):
    """
    Analyze a variation starting with candidate_move from start_board, going variation_depth moves deep.
    For each move in the variation, determine if it is a "critical" (only) move.
    An SVG image is generated after each move.
    
    Returns a dictionary with:
      - "moves": list of SAN moves in the variation,
      - "critical_info": list of tuples (move_san, is_critical, score_diff) for each move,
      - "critical_count": total number of critical moves,
      - "final_board": the board position reached,
      - "explanation": a textual explanation of the main ideas/plans.
    """
    move_info_list = []  # List to store (move_san, is_critical, score_diff) tuples.
    board = start_board.copy()

    # Evaluate the candidate move for criticality before playing it.
    try:
        is_crit, score_diff = evaluate_move_criticality(board, candidate_move, engine, threshold, analysis_time)
    except Exception:
        is_crit, score_diff = False, 0

    try:
        candidate_san = board.san(candidate_move)
    except Exception:
        candidate_san = str(candidate_move)
    move_info_list.append((candidate_san, is_crit, score_diff))
    
    # Play the candidate move and generate an SVG image.
    board.push(candidate_move)
    svg_filename = f"variation_{candidate_san}_step_1.svg"
    generate_position_svg(board, svg_filename)

    # Follow the engine’s principal variation for subsequent moves.
    for i in range(1, variation_depth):
        try:
            info = engine.analyse(board, chess.engine.Limit(time=analysis_time))
            pv = info.get("pv")
            if not pv or len(pv) == 0:
                break
            next_move = pv[0]
            # Evaluate criticality of the next move from the current board position.
            is_crit, score_diff = evaluate_move_criticality(board, next_move, engine, threshold, analysis_time)
            try:
                next_move_san = board.san(next_move)
            except Exception:
                next_move_san = str(next_move)
            move_info_list.append((next_move_san, is_crit, score_diff))
            board.push(next_move)
            svg_filename = f"variation_{candidate_san}_step_{i+1}.svg"
            generate_position_svg(board, svg_filename)
        except Exception:
            break

    # Count total number of critical moves.
    critical_count = sum(1 for _, is_crit, _ in move_info_list if is_crit)

    # Generate an explanation for the final position.
    explanation = generate_plan_explanation(board, engine, analysis_time)
    return {
        "moves": [m for m, _, _ in move_info_list],
        "critical_info": move_info_list,
        "critical_count": critical_count,
        "final_board": board,
        "explanation": explanation
    }


def main():
    # --- 1. START THE ENGINE ---
    engine_path = "./../../../Stockfish-master/src/stockfish"  # Adjust this path to your Stockfish 17 (with NNUE)
    try:
        engine = chess.engine.SimpleEngine.popen_uci(engine_path)
    except Exception as e:
        print("Error starting Stockfish engine:", e)
        return

    # --- 2. GET THE OPENING LINE ---
    opening_name = input("Enter the opening name (e.g. 'Catalan'): ")
    opening_moves = get_opening_moves(opening_name)
    if not opening_moves:
        engine.quit()
        return

    print(f"Using opening line: {opening_moves}")
    board = chess.Board()
    for move in opening_moves:
        try:
            board.push_san(move)
        except Exception as e:
            print(f"Error processing move {move}: {e}")
            engine.quit()
            return

    print("Completed opening line.")
    print("Current position FEN:", board.fen())
    generate_position_svg(board, "opening_line_position.svg")

    # --- 3. FETCH VARIATION CANDIDATES ---
    print("\nFetching candidate moves for variations from the current position...")
    candidates = get_variation_candidates(board, engine, top_moves_count=3, analysis_time=0.5)
    if not candidates:
        print("No candidate moves found for variations.")
        engine.quit()
        return

    # --- 4. ANALYZE EACH VARIATION (with spinner feedback) ---
    variation_depth = 5
    variations = []
    for idx, candidate in enumerate(candidates, start=1):
        try:
            candidate_san = board.san(candidate)
        except Exception:
            candidate_san = str(candidate)
        print(f"\nAnalyzing variation {idx} starting with move: {candidate_san}")
        spinner = Spinner(message="Analyzing variation... ")
        spinner.start()
        var_analysis = analyze_variation(board, candidate, engine, variation_depth, analysis_time=0.5, threshold=50)
        spinner.stop()
        variations.append(var_analysis)
        print(f"Variation {idx} moves: {' '.join(var_analysis['moves'])}")
        print(f"Critical moves in this variation: {var_analysis['critical_count']}")
        # Print detailed criticality info for each move.
        for move_san, is_crit, score_diff in var_analysis['critical_info']:
            crit_msg = "CRITICAL" if is_crit else "Flexible"
            print(f"  Move {move_san}: {crit_msg} (score diff ≈ {score_diff} centipawns)")
        print("Explanation:", var_analysis["explanation"])

    # --- 5. SUMMARY OF VARIATIONS ---
    print("\nSummary of analyzed variations:")
    for idx, var in enumerate(variations, start=1):
        moves_line = ' '.join(var["moves"])
        print(f"Variation {idx}: {moves_line}")
        print(f"Total critical moves: {var['critical_count']}")
        print("Explanation:", var["explanation"])
        print("-" * 40)

    engine.quit()


if __name__ == "__main__":
    main()