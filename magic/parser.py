"""Facilitates parsing of a rueltabel file into an abstract, computer-readable format."""
import re

import bidict

from .common import classes, utils
from .common.utils import print_verbose
from .common.classes import Coord, TabelRange, Variable
from .common.classes.errors import TabelNameError, TabelSyntaxError, TabelValueError, TabelFeatureUnsupported, TabelException


class AbstractTabel:
    """
    Creates an abstract, Golly-transferrable representation of a ruelfile's @TABEL section.
    """
    __rCARDINALS = 'NE|NW|SE|SW|N|E|S|W'
    __rVAR = r'[({](?:\w*\s*(?:,|\.\.)\s*)*(?:\w|(?:\.\.\.)?)*[})]'
    
    _rASSIGNMENT = re.compile(rf'\w+?\s*=\s*{__rVAR}')
    _rBINDMAP = re.compile(rf'\[[0-8](?::\s*?(?:{__rVAR}|[^_]\w+?))?\]')
    _rCARDINAL = re.compile(rf'\b(\[)?({__rCARDINALS})((?(1)\]))\b')
    _rPTCD = re.compile(rf'\b({__rCARDINALS})(?::(\d+)\b|\[((?:{__rCARDINALS})\s*:)?\s*(\w+|{__rVAR})]\B)')
    _rRANGE = re.compile(r'\d+? *?\.\. *?\d+?')
    _rTRANSITION = re.compile(
      rf'((?:(?:\d|{__rCARDINALS})'                  # Meaningless cardinal direction before state (like ", NW 2,")
      rf'(?:\s*\.\.\s*(?:\d|{__rCARDINALS}))?\s+)?'  # Range of cardinal directions (like ", N..NW 2,")
       r'(?:\d+|'                                    # Normal state (like ", 2,")
       r'(?:[({](?:\w*\s*(?:,|\.\.)\s*)*\w+[})]'     # Variable literal (like ", (1, 2..2, 3),") w/o "..." at end
       r'|[A-Za-z]+)'                                # Variable name (like ", aaaa,")
      rf'|\[(?:(?:\d|{__rCARDINALS})\s*:\s*)?'       # Or a mapping, which starts with either a number or the equivalent cardinal direction
      rf'(?:{__rVAR}|[A-Za-z]+)\]))'                 # ...and then has either a variable name or literal (like ", [S: (1, 2, ...)],")
       r'(,)?(?(2)\s*)'                              # Finally, an optional comma and whitespace after it. Last term has no comma.
      )
    _rVAR = re.compile(__rVAR)
    
    CARDINALS = {
      'Moore': {'N': 1, 'NE': 2, 'E': 3, 'SE': 4, 'S': 5, 'SW': 6, 'W': 7, 'NW': 8},
      'vonNeumann': {'N': 1, 'E': 2, 'S': 3, 'W': 4},
      'hexagonal': {'N': 1, 'E': 2, 'SE': 3, 'S': 4, 'W': 5, 'NW': 6}
      }
    POSITIONS = {
      'E': Coord((1, 0)),
      'N': Coord((0, 1)),
      'NE': Coord((1, 1)),
      'NW': Coord((-1, 1)),
      'S': Coord((0, -1)),
      'SE': Coord((1, -1)),
      'SW': Coord((-1, -1)),
      'W': Coord((-1, 0))
      }
    
    def __init__(self, tbl, start=0):
        self._tbl = tbl
        
        self.vars = bidict.bidict()  # {Variable(name) | str(name) :: tuple(value)}
        self.var_all = ()  # replaces self.vars['__all__']
        self.directives = {}
        self.transitions = []
        
        _assignment_start = self._extract_directives(start)
        _transition_start = self._extract_initial_vars(_assignment_start)
        
        
        self.cardinals = self._parse_directives()
        print_verbose(
          '\b'*4 + 'PARSED: directives & var assignments',
          ['\b\bdirectives:', self.directives, '\b\bvars:', self.vars],
          pre='    ', sep='\n', end='\n'
          )
        
        self._parse_transitions(_transition_start)
        print_verbose(
          '\b'*4 + 'PARSED: transitions & PTCDs',
          ['\b\btransitions (before binding):', self.transitions, '\b\bvars:', self.vars],
          pre='    ', sep='\n', end='\n\n'
          )
    
    def __iter__(self):
        return iter(self._tbl)
    
    def __getitem__(self, item):
        return self._tbl[item]
    
    def _cardinal_sub(self, match):
        try:
            return f"{match[1] or ''}{self.cardinals[match[2]]}{match[3]}"
        except KeyError:
            raise KeyError(match[2])
    
    def _parse_variable(self, var: str, *, mapping=False, ptcd=False):
        """
        var: a variable literal
        
        return: var, but as a tuple with any references substituted for their literal values
        """
        if var.isalpha():
            return self.vars[var]
        cop = []
        for state in map(str.strip, var[1:-1].split(',')):  # var[1:-1] cuts out (parens)/{braces}
            if state.isdigit():
                cop.append(int(state))
            elif self._rRANGE.match(state):
                try:
                    cop.extend(TabelRange(state))
                except ValueError as e:
                    raise SyntaxError((str(e).split("'")[1], state)) from None
            elif mapping and state == '...' or ptcd and state in ('...', '_'):
                cop.append(state)
            else:
                try:
                    cop.extend(self.vars[state])
                except KeyError:
                    raise NameError(state) from None
        return tuple(cop)
    
    def _extract_directives(self, start):
        """
        Gets directives from top of ruelfile.
        
        return: the line number at which var assignment starts.
        """
        lno = start
        for lno, line in enumerate((i.split('#')[0].strip() for i in self), start):
            if not line:
                continue
            if self._rASSIGNMENT.fullmatch(line):
                break
            directive, value = map(str.strip, line.split(':'))
            self.directives[directive] = value.replace(' ', '')
        return lno
    
    def _parse_directives(self):
        """
        Parses extracted directives to understand their values.
        """
        try:
            self.var_all = tuple(range(int(self.directives['states'])))
            cardinals = self.CARDINALS.get(self.directives['neighborhood'])
            if cardinals is None:
                raise TabelValueError(None, f"Invalid neighborhood {self.directives['neighborhood']!r} declared")
            if 'symmetries' not in self.directives:
                raise KeyError("'symmetries'")
        except KeyError as e:
            name = str(e).split("'")[1]
            raise TabelNameError(None, f'{name!r} directive not declared')
        return cardinals
    
    def _extract_initial_vars(self, start):
        """
        start: line number to start from
        
        Iterates through tabel and gathers all explicit variable declarations.
        
        return: line number at which transition declaration starts
        """
        lno = start
        tblines = ((idx, stmt.strip()) for idx, line in enumerate(self[start:], start) for stmt in line.split('#')[0].split(';'))
        for lno, decl in tblines:
            if utils.globalmatch(self._rTRANSITION, decl):
                break
            if not decl:
                continue
            if not self._rASSIGNMENT.fullmatch(decl):
                raise TabelSyntaxError(lno, f'Invalid syntax in variable declaration (please let Wright know ASAP if this is incorrect)')
            name, value = map(str.strip, decl.split('='))

            try:
                var = self._parse_variable(value)
            except NameError as e:
                if not str(e):  # Means two consecutive commas, or a comma at the end of a literal
                    raise TabelSyntaxError(lno, 'Invalid comma placement in variable declaration')
                adj = 'undefined' if str(e).isalpha() else 'invalid'
                raise TabelNameError(lno, f"Declaration of variable {name!r} references {adj} name '{e}'")
            except SyntaxError as e:
                bound, range_ = e.msg
                raise TabelSyntaxError(lno, f"Bound '{bound}' of range {range_} is not an integer")
            
            if name == '__all__':  # the special var
                self.var_all = var
                continue
            if not name.isalpha():
                raise TabelSyntaxError(
                  lno,
                  f'Variable name {name!r} contains nonalphabetical character {next(i for i in name if not i.isalpha())!r}',
                  )
            try:
                self.vars[Variable(name)] = var
            except bidict.ValueDuplicationError:
                raise TabelValueError(lno, f"Value {value} is already assigned to variable '{self.vars.inv[var]}'")
        self.vars.on_dup_val = bidict.IGNORE
        return lno
    
    def _extract_ptcd_var(self, tr, match, lno):
        """
        tr: a transition
        match: matched PTCD (regex match object)
        lno: current line number
        
        Parses the 'variable' segment of a PTCD.
        
        return: PTCD's variables
        """
        cdir = match[1]
        copy_to = tr[self.cardinals[cdir]]
        if match[2] is not None:  # Means it's a simple "CD:state" instead of a "CD[variable]"
            return match[1], copy_to, map(int, match[2])
        try:
            _map_to, map_to = [], self._parse_variable(match[4], ptcd=True)
        except NameError as e:
            raise TabelNameError(lno, f"PTCD references undefined name '{e}'")
        except SyntaxError as e:
                bound, range_ = e.msg
                raise TabelSyntaxError(lno, f"Bound '{bound}' of range {range_} is not an integer")
        for idx, state in enumerate(map_to):
            if state == '_':  # Leave as is (indicated by a None value)
                state = None
            if state == '...':  # Fill out with preceding element (this should be generalized to all mappings actually)
                # TODO: Allow placement of ... in the middle of an expression (to be filled in from both sides)
                _map_to.append(range(idx, len(copy_to)))  # Check isinstance(range) to determine whether to generate anonymous variable
                break
            _map_to.append(state)
        if len(copy_to) > sum(len(i) if isinstance(i, range) else 1 for i in _map_to):
            raise TabelValueError(
              lno,
              f"Variable at index {self.cardinals[cdir]} in PTCD (direction {cdir})"
              " mapped to a smaller variable. Maybe add a '...' to the latter?"
              )
        return match[1], copy_to, _map_to
    
    def _make_transition(self, tr: list, source_cd: str, initial, result):
        """
        tr: Original transition
        source_cd: Cardinal direction as a letter or pair thereof. (N, S, SW, etc)
        initial: What to 
        
        Builds a transition from segments of a PTCD.
        """
        new_tr = [initial, *['__all__']*len(self.cardinals), result]
        orig = Coord(self.POSITIONS[source_cd]).opp  # creates S -> N, E -> W, etc.
        try:
            new_tr[self.cardinals[orig.name]] = tr[0]
        except KeyError:
            pass
        try:
            new_tr[self.cardinals[orig.cw.name]] = tr[self.cardinals[orig.cw.move(source_cd).name]]
        except KeyError:
            pass
        try:
            new_tr[self.cardinals[orig.ccw.name]] = tr[self.cardinals[orig.ccw.move(source_cd).name]]
        except KeyError:
            pass
        if not orig.diagonal():  # will execute if the PTCD is orthogonal & not diagonal
            try:
                new_tr[self.cardinals[orig.cw(2).name]] = tr[self.cardinals[orig.cw(3).name]]
            except KeyError:
                pass
            try:
                new_tr[self.cardinals[orig.ccw(2).name]] = tr[self.cardinals[orig.ccw(3).name]]
            except KeyError:
                pass
        return new_tr
        
    def _parse_ptcd(self, tr, match, lno):
        """
        tr: a fully-parsed transition statement
        ptcd: a PTCD
        variables: global dict of variables
        
        PTCDs can be
            CD[(var_literal)]
            CD[variable]
        Or
            CD[CD: (var_literal)]
            CD[CD: variable]
        
        return: PTCD expanded into its full transition(s)
        """
        if match[3] is not None:
            raise TabelFeatureUnsupported(lno, 'PTCDs that copy neighbor states are not yet supported')
        cd_idx, copy_to, map_to = self._extract_ptcd_var(tr, match, lno)
        # Start expansion to transitions
        transitions = []
        for idx, (initial, result) in enumerate(zip(copy_to, map_to)):
            if result is None:
                continue
            # If the result is a "..." fillout
            if isinstance(result, range):
                if map_to[idx-1] is None:  # Nothing more to add
                    break
                new_initial = copy_to[result[0]:]
                transitions.append(self._make_transition(tr, cd_idx, new_initial, map_to[idx-1]))
                self.vars[Variable.random_name()] = new_initial
                break
            transitions.append(self._make_transition(tr, cd_idx, initial, result))
        return transitions
    
    def _parse_transitions(self, start):
        """
        start: line number to start on
        Parses all the ruel's transitions into a list in self.transitions.
        """
        lno = start
        for lno, line in enumerate((i.split('#')[0].strip() for i in self[start:]), start):
            if not line:
                continue
            if self._rASSIGNMENT.match(line):
                raise TabelSyntaxError(lno, 'Variable declaration after transitions')
            napkin, ptcds = map(str.strip, line.partition('->')[::2])
            try:
                napkin = [self._rCARDINAL.sub(self._cardinal_sub, i.strip()) for i, _ in self._rTRANSITION.findall(napkin)]
            except KeyError as e:
                raise TabelValueError(
                  lno,
                  f"Invalid cardinal direction {e} for {self.directives['symmetries']!r} symmetry"
                  )
            napkin = utils.expand_tr(napkin)
            # Parse napkin into proper range of ints
            for idx, elem in enumerate(napkin):
                if elem.isdigit():
                    napkin[idx] = int(elem)
                elif self._rVAR.match(elem):
                    self.vars[Variable.random_name()] = self._parse_variable(elem)
                elif not self._rBINDMAP.match(elem):  # leave mappings and bindings untouched for now
                    try:
                        napkin[idx] = self.vars[elem]
                    except KeyError:
                        raise TabelNameError(lno, f'Transition references undefined name {elem!r}')
            ptcds = [tr for ptcd in self._rPTCD.finditer(ptcds) for tr in self._parse_ptcd(napkin, ptcd, lno=lno)]
            self.transitions.extend([napkin, *ptcds])


class AbstractColors:
    """
    Parses a ruelfile's color format into something abstract &
    transferrable into Golly syntax.
    """
    def __init__(self, *args, **kwargs):
        pass


def parse(fp):
    """
    fp: file pointer to a full .ruel file
    
    return: file, sectioned into dict with tabel and
    colors as convertable representations
    """
    parts, lines = {}, {}
    segment = None
    
    for lno, line in enumerate(map(str.strip, fp), 1):
        if line.startswith('@'):
            # @RUEL, @TABEL, @COLORS, ...
            segment, *name = line.split(None, 1)
            parts[segment], lines[segment] = name, lno
            continue
        parts[segment].append(line)
    
    try:
        parts['@TABLE'] = AbstractTabel(parts['@TABEL'])
    except KeyError:
        raise TabelNameError(None, "No '@TABEL' segment found")
    except TabelException as exc:
        if exc.lno is None:
            raise exc.__class__(exc.lno, exc.msg)
        raise exc.__class__(exc.lno, exc.msg, parts['@TABEL'], lines['@TABEL'])
    
    try:
        parts['@COLORS'] = AbstractColors(parts['@COLORS'])
    except KeyError:
        pass
    except TabelException as exc:
        raise exc.__class__(exc.lno, exc.msg, parts['@COLORS'], lines['@COLORS'])
    return parts
