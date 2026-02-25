import enum
import logging
import operator
from argparse import ArgumentParser
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import reduce

import tree_sitter_epics_cmd as tse
from lsprotocol import types
from lsprotocol.types import SemanticTokenModifiers, SemanticTokens, SemanticTokenTypes
from pygls.cli import start_server
from pygls.lsp.server import LanguageServer
from pygls.workspace import TextDocument
from tree_sitter import Language, Parser, Point, Query, QueryCursor, Tree

from . import __version__

# Declare the SemanticTokenTypes this server will provide
# Note these must match the types used in HIGHLIGHTS_QUERY_MAPPING
# TODO: Make that typing more secure
token_types = [
    SemanticTokenTypes.Comment,
    SemanticTokenTypes.Function,
    SemanticTokenTypes.Parameter,
    SemanticTokenTypes.Label,
    SemanticTokenTypes.Variable,
    SemanticTokenTypes.Macro,
]


# Declare the SemanticTokenModifiers this server will provide.
# Note they are sent as a bitfield, hence the use of IntFlag
class TokenModifier(enum.IntFlag):
    defaultLibrary = enum.auto()  # noqa: N815 the LSP uses mixed case so we will too


@dataclass
class Token:
    line: int
    offset: int
    text: str

    tok_type: SemanticTokenTypes
    tok_modifiers: list[SemanticTokenModifiers] = field(
        default_factory=list[SemanticTokenModifiers]
    )


# Mapping TreeSitter query capture group names to Language Server Protocol equivalents
@dataclass
class QueryTypeToSemanticTokenType:
    query_type: str
    semantic_token_type: SemanticTokenTypes


# Mapping the query types index to their string and their SemanticTokenType
# See tse.HIGHLIGHTS_QUERY.
HIGHLIGHTS_QUERY_MAPPING = {
    0: QueryTypeToSemanticTokenType("comment", SemanticTokenTypes.Comment),
    1: QueryTypeToSemanticTokenType("string", SemanticTokenTypes.Parameter),
    2: QueryTypeToSemanticTokenType("string.special", SemanticTokenTypes.Variable),
    3: QueryTypeToSemanticTokenType("string.special", SemanticTokenTypes.Macro),
    4: QueryTypeToSemanticTokenType("function", SemanticTokenTypes.Function),
    5: QueryTypeToSemanticTokenType("variable.parameter", SemanticTokenTypes.Parameter),
    6: QueryTypeToSemanticTokenType("variable.parameter", SemanticTokenTypes.Parameter),
    7: QueryTypeToSemanticTokenType("punctuation.bracket", SemanticTokenTypes.Label),
    8: QueryTypeToSemanticTokenType("punctuation.delimiter", SemanticTokenTypes.Label),
    9: QueryTypeToSemanticTokenType("operator", SemanticTokenTypes.Label),
}


class SemanticTokensServer(LanguageServer):
    """Language server demonstrating the semantic token methods from the LSP
    specification."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tokens: dict[str, SemanticTokens] = {}
        self.lang = Language(tse.language())
        # TODO: Not sure if supposed to re-use parser in this way
        self.parser = Parser(self.lang)

    def parse(self, doc: TextDocument):
        """Convert the given document into a list of tokens"""
        tree = self.lex(doc)

        tokens = self.create_tokens(tree)

        self.tokens[doc.uri] = tokens

    def create_tokens(self, tree: Tree) -> SemanticTokens:
        """Convert the TreeSitter token tree into the list of LSP tokens"""

        # Token data is just one long list, where each group of 5 ints represents one
        # token
        data: list[int] = []

        lang = Language(tse.language())
        query = Query(lang, tse.HIGHLIGHTS_QUERY)

        query_cursor = QueryCursor(query)

        # Note that the order of the matches is important; SemanticTokens line and
        # offset parameters are in relation to the previous token. This is why we use
        # .matches() rather than .captures() as the "captures" are unordered.
        matches = query_cursor.matches(tree.root_node)

        # Keep track of the location of the previous Node, for offset calculations
        prev_point = Point(0, 0)

        # Matches are a list of tuples, where the first item is the index of the query
        # search that matches. The hard-coded strings all come from
        # tse.HIGHLIGHTS_QUERY, but unfortunately I don't think there's a way to
        # programmatically retrieve them.
        # TODO: Define my own query so I can!
        for match in matches:
            match_type = match[0]

            nodes = match[1][HIGHLIGHTS_QUERY_MAPPING[match_type].query_type]
            for node in nodes:
                # Note I only expect there to be a single node, but this code should
                # handle an arbitrary number
                if not node.text:
                    # No text means nothing to highlight
                    # TODO: Log messages everywhere!
                    continue

                line = node.start_point.row - prev_point.row

                if node.start_point.row != prev_point.row:
                    # Moving to a new row must reset offset
                    prev_point = Point(0, 0)

                offset = node.start_point.column - prev_point.column

                assert line >= 0
                assert offset >= 0

                token = Token(
                    line=line,
                    offset=offset,
                    text=node.text.decode(),
                    tok_type=HIGHLIGHTS_QUERY_MAPPING[match_type].semantic_token_type,
                )

                prev_point = node.start_point

                # TODO: Still have to do token modifiers; not sure there's any except
                # defaultLibrary? That'll need the list of inbuilt functions...

                data.extend(
                    [
                        token.line,
                        token.offset,
                        len(token.text),
                        # TODO: TreeSitter does give us the end_point, could use that
                        # instead of doing len()
                        token_types.index(token.tok_type),
                        reduce(operator.or_, token.tok_modifiers, 0),
                    ]
                )

        return SemanticTokens(data)

    def lex(self, doc: TextDocument) -> Tree:
        """Convert the given document into a TreeSitter Tree"""

        contents = doc.lines

        contents_bytes = b"".join(i.encode() for i in contents)

        return self.parser.parse(contents_bytes)


server = SemanticTokensServer("semantic-tokens-server", "v1")


@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
def did_open(ls: SemanticTokensServer, params: types.DidOpenTextDocumentParams):
    """Parse each document when it is opened"""
    doc = ls.workspace.get_text_document(params.text_document.uri)
    ls.parse(doc)


@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls: SemanticTokensServer, params: types.DidOpenTextDocumentParams):
    """Parse each document when it is changed"""
    doc = ls.workspace.get_text_document(params.text_document.uri)
    ls.parse(doc)


@server.feature(
    types.TEXT_DOCUMENT_SEMANTIC_TOKENS_FULL,
    types.SemanticTokensLegend(
        token_types=token_types,
        token_modifiers=[m.name for m in TokenModifier],
    ),
)
def semantic_tokens_full(ls: SemanticTokensServer, params: types.SemanticTokensParams):
    """Return the semantic tokens for the entire document"""
    tokens = ls.tokens.get(params.text_document.uri, SemanticTokens(data=[]))

    return tokens


__all__ = ["main"]


def main(args: Sequence[str] | None = None) -> None:
    """Argument parser for the CLI."""
    parser = ArgumentParser()
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=__version__,
    )
    parser.parse_args(args)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    start_server(server)


if __name__ == "__main__":
    main()
