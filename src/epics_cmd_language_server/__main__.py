import enum
import logging
import operator
from argparse import ArgumentParser
from dataclasses import dataclass, field
from functools import reduce

import tree_sitter_epics_cmd as tse
from lsprotocol import types
from lsprotocol.types import (
    Diagnostic,
    DiagnosticSeverity,
    SemanticTokenModifiers,
    SemanticTokens,
    SemanticTokenTypes,
)
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


EPICS_DOCS_BASE_URL = "https://docs.epics-controls.org/en/latest/appdevguide"
IOC_DOCS_URL = EPICS_DOCS_BASE_URL + "/" + "IOCTestFacilities.html#"


@dataclass
class EpicsFunction:
    """Encapsulates all information about a particular inbuilt function"""

    name: str
    help_str: str | None = None
    web_link: str | None = None
    signature: types.SignatureInformation | None = None


INBUILT_FUNCTIONS = [
    EpicsFunction("dbDumpDevice", help_str=None, web_link=None),
    EpicsFunction("dbDumpDriver", help_str=None, web_link=None),
    EpicsFunction("dbDumpField", help_str=None, web_link=None),
    EpicsFunction("dbDumpFunction", help_str=None, web_link=None),
    EpicsFunction("dbDumpMenu", help_str=None, web_link=None),
    EpicsFunction("dbDumpPath", help_str=None, web_link=None),
    EpicsFunction("dbDumpRecord", help_str=None, web_link=None),
    EpicsFunction("dbDumpRecordType", help_str=None, web_link=None),
    EpicsFunction("dbDumpRegistrar", help_str=None, web_link=None),
    EpicsFunction("dbDumpVariable", help_str=None, web_link=None),
    EpicsFunction("dbLoadDatabase", help_str=None, web_link=None),
    EpicsFunction("dbLoadRecords", help_str=None, web_link=None),
    EpicsFunction("dbLoadTemplate", help_str=None, web_link=None),
    EpicsFunction("dba", help_str=None, web_link=None),
    EpicsFunction("dbap", help_str=None, web_link=None),
    EpicsFunction("dbb", help_str=None, web_link=None),
    EpicsFunction("dbc", help_str=None, web_link=None),
    EpicsFunction("dbcar", help_str=None, web_link=None),
    EpicsFunction("dbd", help_str=None, web_link=None),
    EpicsFunction("dbel", help_str=None, web_link=None),
    EpicsFunction("dbgf", help_str=None, web_link=None),
    EpicsFunction("dbgrep", help_str=None, web_link=None),
    EpicsFunction("dbhcr", help_str=None, web_link=None),
    EpicsFunction("dbior", help_str=None, web_link=None),
    EpicsFunction(
        "dbl",
        help_str="""
This command prints the names of records in the run time database. If <record type> is empty (""), "*", or not specified, all records are listed. If <record type> is specified, then only the names of the records of that type are listed.

If <field list> is given and not empty then the values of the fields specified are also printed.
    """,  # noqa: E501
        web_link=IOC_DOCS_URL + "dbl",
        signature=types.SignatureInformation(
            label='dbl("<record type>","<field list>")',
            documentation=(
                "This command prints the names of records in the run time database.",
            ),
            parameters=[
                types.ParameterInformation(label="record type"),
                types.ParameterInformation(label="field list"),
            ],
        ),
    ),
    EpicsFunction("dbla", help_str=None, web_link=None),
    EpicsFunction("dblsr", help_str=None, web_link=None),
    EpicsFunction("dbnr", help_str=None, web_link=None),
    EpicsFunction("dbp", help_str=None, web_link=None),
    EpicsFunction("dbpf", help_str=None, web_link=None),
    EpicsFunction("dbpr", help_str=None, web_link=None),
    EpicsFunction("dbs", help_str=None, web_link=None),
    EpicsFunction("dbsr", help_str=None, web_link=None),
    EpicsFunction("dbstat", help_str=None, web_link=None),
    EpicsFunction("dbtgf", help_str=None, web_link=None),
    EpicsFunction("dbtpf", help_str=None, web_link=None),
    EpicsFunction("dbtpn", help_str=None, web_link=None),
    EpicsFunction("dbtr", help_str=None, web_link=None),
    EpicsFunction("echo", help_str=None, web_link=None),
    EpicsFunction("epicsEnvSet", help_str=None, web_link=None),
    EpicsFunction("epicsEnvShow", help_str=None, web_link=None),
    EpicsFunction("epicsEnvUnset", help_str=None, web_link=None),
    EpicsFunction("errlog", help_str=None, web_link=None),
    EpicsFunction("errlogInit", help_str=None, web_link=None),
    EpicsFunction("errlogInit2", help_str=None, web_link=None),
    EpicsFunction("help", help_str=None, web_link=None),
    EpicsFunction("iocInit", help_str=None, web_link=None),
    EpicsFunction("iocLogInit", help_str=None, web_link=None),
    EpicsFunction("iocLogPrefix", help_str=None, web_link=None),
    EpicsFunction("iocLogShow", help_str=None, web_link=None),
    EpicsFunction("iocPause", help_str=None, web_link=None),
    EpicsFunction("iocRun", help_str=None, web_link=None),
    EpicsFunction("iocshCmd", help_str=None, web_link=None),
    EpicsFunction("iocshLoad", help_str=None, web_link=None),
    EpicsFunction("iocshRun", help_str=None, web_link=None),
    EpicsFunction("pwd", help_str=None, web_link=None),
    EpicsFunction("system", help_str=None, web_link=None),
    EpicsFunction("traceIocInit", help_str=None, web_link=None),
    EpicsFunction("var", help_str=None, web_link=None),
]


class EpicsCmdLanguageServer(LanguageServer):
    """Language server demonstrating the semantic token methods from the LSP
    specification."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tokens: dict[str, SemanticTokens] = {}
        self.trees: dict[str, Tree] = {}
        self.lang = Language(tse.language())
        # TODO: Not sure if supposed to re-use parser in this way
        self.parser = Parser(self.lang)
        self.diagnostics: dict[str, tuple[int | None, list[Diagnostic]]] = {}

    def parse(self, doc: TextDocument) -> Tree:
        """Convert the given document into TreeParser Tree"""
        tree = self.lex(doc)
        self.trees[doc.uri] = tree

        self.create_diagnostics(tree, doc)

        return tree

    def create_tokens(self, doc: TextDocument) -> SemanticTokens:
        """Convert the TreeSitter token tree into the list of LSP tokens"""

        try:
            tree = self.trees[doc.uri]
        except KeyError:
            # No tree, re-parse document
            tree = self.parse(doc)

        # Token data is just one long list, where each group of 5 ints represents one
        # token
        data: list[int] = []

        query = Query(self.lang, tse.HIGHLIGHTS_QUERY)
        query_cursor = QueryCursor(query)

        # Note that the order of the matches is important; SemanticTokens line and
        # offset parameters are in relation to the previous token. This is why we use
        # .matches() rather than .captures() as the "captures" are unordered.
        matches = query_cursor.matches(tree.root_node)

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

        tokens = SemanticTokens(data)
        self.tokens[doc.uri] = tokens

        return tokens

    def lex(self, doc: TextDocument) -> Tree:
        """Convert the given document into a TreeSitter Tree"""

        contents = doc.lines

        contents_bytes = b"".join(i.encode() for i in contents)

        return self.parser.parse(contents_bytes)

    def create_diagnostics(self, tree: Tree, doc: TextDocument):
        """Parse the syntax tree to identify any issues"""

        diagnostics: list[Diagnostic] = []

        # This parser does one of two things when it encounters a problem:
        # 1. Create an ERROR node and continue parsing
        # 2. Attempt to fix the error by inserting node(s) into the parsed tree.
        # This query will find both of these cases
        query_str = """
(ERROR) @error-node
(MISSING) @missing-node
"""
        query = Query(self.lang, query_str)
        query_cursor = QueryCursor(query)

        for k, v in query_cursor.captures(tree.root_node).items():
            if k == "missing-node":
                for node in v:
                    next_node = node.next_sibling
                    if next_node and next_node.type == "comment":
                        # End of line comments are not advisable
                        message = (
                            "End of line comments are actually parameters to the "
                            "function. They should not be used."
                        )
                        diagnostics.append(
                            types.Diagnostic(
                                message=message,
                                severity=DiagnosticSeverity.Warning,
                                range=types.Range(
                                    start=types.Position(
                                        line=next_node.start_point.row,
                                        character=next_node.start_point.column,
                                    ),
                                    end=types.Position(
                                        line=next_node.end_point.row,
                                        character=next_node.end_point.column,
                                    ),
                                ),
                            )
                        )

                    # This one appears to be a bug in the parser, see https://github.com/minijackson/tree-sitter-epics-cmd/issues/7
                    if node.text == b"":
                        pass

            elif k == "error-node":
                for node in v:
                    if node.text == b"(":
                        # Unclosed parenthesis error

                        # There is a bug with the parser that makes this situation
                        # difficult to deal with:
                        # https://github.com/minijackson/tree-sitter-epics-cmd/issues/8
                        # So for now we skip it
                        # diagnostics.append(
                        #     types.Diagnostic(
                        #         message="Unclosed parenthesis",
                        #         severity=DiagnosticSeverity.Error,
                        #         range=err_range,
                        #     )
                        # )
                        pass
                    elif node.start_point.row != node.end_point.row:
                        # We never expect multi-line tokens, so mark the whole length as
                        # an error. This currently happens with unclosed quotes and
                        # unclosed macro parenthesis
                        diagnostics.append(
                            types.Diagnostic(
                                message="Unclosed quotations",
                                severity=DiagnosticSeverity.Error,
                                range=types.Range(
                                    start=types.Position(
                                        line=node.start_point.row,
                                        character=node.start_point.column,
                                    ),
                                    end=types.Position(
                                        line=node.start_point.row + 1, character=0
                                    ),  # Marks until the end of the start row
                                ),
                            )
                        )

        self.diagnostics[doc.uri] = (doc.version, diagnostics)


server = EpicsCmdLanguageServer("EPICS-cmd-language-server", "v1")


@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
def did_open(ls: EpicsCmdLanguageServer, params: types.DidOpenTextDocumentParams):
    """Parse each document when it is opened"""
    doc = ls.workspace.get_text_document(params.text_document.uri)
    ls.parse(doc)

    for uri, (version, diagnostics) in ls.diagnostics.items():
        ls.text_document_publish_diagnostics(
            types.PublishDiagnosticsParams(
                uri=uri,
                version=version,
                diagnostics=diagnostics,
            )
        )


@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls: EpicsCmdLanguageServer, params: types.DidOpenTextDocumentParams):
    """Parse each document when it is changed"""
    doc = ls.workspace.get_text_document(params.text_document.uri)
    ls.parse(doc)

    for uri, (version, diagnostics) in ls.diagnostics.items():
        ls.text_document_publish_diagnostics(
            types.PublishDiagnosticsParams(
                uri=uri,
                version=version,
                diagnostics=diagnostics,
            )
        )


@server.feature(
    types.TEXT_DOCUMENT_SEMANTIC_TOKENS_FULL,
    types.SemanticTokensLegend(
        token_types=token_types,
        token_modifiers=[m.name for m in TokenModifier],
    ),
)
def semantic_tokens_full(
    ls: EpicsCmdLanguageServer, params: types.SemanticTokensParams
):
    """Return the semantic tokens for the entire document"""
    # tokens = ls.tokens.get(params.text_document.uri, SemanticTokens(data=[]))
    doc = ls.workspace.get_text_document(params.text_document.uri)
    tokens = ls.create_tokens(doc)

    return tokens


@server.feature(
    types.TEXT_DOCUMENT_COMPLETION,
    types.CompletionOptions(),
)
def completions(ls: EpicsCmdLanguageServer, params: types.CompletionParams):
    completions: list[types.CompletionItem] = []
    for cmd in INBUILT_FUNCTIONS:
        completions.append(types.CompletionItem(label=cmd.name))

    return completions


@server.feature(types.TEXT_DOCUMENT_HOVER)
def hover(ls: LanguageServer, params: types.HoverParams):
    pos = params.position
    document_uri = params.text_document.uri
    document = ls.workspace.get_text_document(document_uri)

    try:
        line = document.lines[pos.line]
    except IndexError:
        return None

    hover_text: str | None = None
    for cmd in INBUILT_FUNCTIONS:
        if cmd.name in line:
            hover_text = cmd.help_str

    if not hover_text:
        return

    return types.Hover(
        contents=types.MarkupContent(
            kind=types.MarkupKind.Markdown,
            value=hover_text,
        ),
        range=types.Range(
            start=types.Position(line=pos.line, character=0),
            end=types.Position(line=pos.line + 1, character=0),
        ),
    )


@server.feature(
    types.TEXT_DOCUMENT_SIGNATURE_HELP,
    types.SignatureHelpOptions(trigger_characters=["(", " "]),
)
def signature_help(
    ls: LanguageServer, params: types.SignatureHelpParams
) -> types.SignatureHelp | None:

    pos = params.position
    document_uri = params.text_document.uri
    document = ls.workspace.get_text_document(document_uri)

    try:
        line = document.lines[pos.line]
    except IndexError:
        return None

    for cmd in INBUILT_FUNCTIONS:
        if cmd.name in line and cmd.signature:
            return types.SignatureHelp(
                signatures=[cmd.signature], active_signature=0, active_parameter=0
            )

    return None


__all__ = ["main"]


def main() -> None:
    """Argument parser for the CLI."""

    parser = ArgumentParser(description="Start an EPICS language server instance")

    parser.add_argument(
        "launch",
        choices=["IO", "TCP", "WS"],
        default="IO",
        help="Define the launch method of the server",
    )

    parser.add_argument("--host", default="127.0.0.1", help="bind to this address")
    parser.add_argument("--port", type=int, default=8888, help="bind to this port")
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=__version__,
    )

    arguments = parser.parse_args()

    # TODO: Command line logging config
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if arguments.launch == "TCP":
        server.start_tcp(arguments.host, arguments.port)
    elif arguments.launch == "WS":
        server.start_ws(arguments.host, arguments.port)
    elif arguments.launch == "IO":
        server.start_io()
    else:
        raise ValueError("Invalid launch type detected")


if __name__ == "__main__":
    main()
