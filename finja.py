import argparse
import codecs
import hashlib
import math
import os
import sqlite3
import stat
import sys

import six
from binaryornot.check import is_binary

import finja_shlex as shlex

if six.PY2:
    def blob(x):
        return sqlite3.Binary(x)
else:
    def blob(x):
        return x

_cache_size = 1024 * 1024

_db_cache = None

_shlex_settings = {
    '.default': {
        'commenters': ""
    },
    '.override0': {
    },
    '.override1': {
        'whitespace_split': True
    },
    '.override2': {
        'quotes': ""
    },
}

_ignore_dir = set([
    ".git",
    ".svn"
])

_args = None


class TokenDict(dict):
    def __init__(self, db, *args, **kwargs):
        super(TokenDict, self).__init__(*args, **kwargs)
        self.db = db

    def __missing__(self, key):
        with self.db:
            cur = self.db.cursor()
            res = cur.execute("""
                SELECT
                    id
                FROM
                    token
                WHERE
                    string = ?;
            """, (blob(key),)).fetchall()
            if res:
                ret = res[0][0]
            else:
                cur.execute("""
                    INSERT INTO
                        token(string)
                    VALUES
                        (?);
                """, (blob(key),))
                ret = cur.lastrowid
        self[key] = ret
        return ret


def cleanup(string):
    if len(string) <= 16:
        return string.lower()
    return hashlib.md5(string.lower().encode("UTF-8")).digest()


def get_line(file_path, lineno):
    line = "!! Bad encoding"
    try:
        with codecs.open(file_path, "r", encoding="UTF-8") as f:
            for _ in range(lineno):
                line = f.readline()
    except UnicodeDecodeError:
        pass
    except IOError:
        line = "!! File not found"
    return line


def get_db(create=False):
    global _db_cache
    if _db_cache:
        return _db_cache  # noqa
    exists = os.path.exists("FINJA")
    if not (create or exists):
        raise ValueError("Could not find FINJA")
    connection = sqlite3.connect("FINJA")  # noqa
    if not exists:
        connection.execute("""
            CREATE TABLE
                finja(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id INTEGER,
                    file_id INTEGER,
                    line INTEGER
                );
        """)
        connection.execute("""
            CREATE INDEX finja_token_id_idx ON finja (token_id);
        """)
        connection.execute("""
            CREATE INDEX finja_file_idx ON finja (file_id);
        """)
        connection.execute("""
            CREATE TABLE
                token(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    string BLOB
                );
        """)
        connection.execute("""
            CREATE INDEX token_string_idx ON token (string);
        """)
        connection.execute("""
            CREATE TABLE
                file(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT,
                    inode INTEGER,
                    found INTEGER DEFAULT 1
                );
        """)
        connection.execute("""
            CREATE INDEX file_path_idx ON file (path);
        """)
    connection.commit()
    _db_cache = (connection, TokenDict(connection))
    return _db_cache


def apply_shlex_settings(pass_, ext, lex):
    to_apply = [_shlex_settings['.default']]
    if ext in _shlex_settings:
        to_apply.append(_shlex_settings[ext])
    to_apply.append(
        _shlex_settings['.override%s' % pass_]
    )
    for settings in to_apply:
        for key in settings.keys():
            setattr(lex, key, settings[key])


def index_file(db, file_path, update = False):
    con        = db[0]
    token_dict = db[1]
    # Bad symlinks etc.
    try:
        stat_res = os.stat(file_path)
    except OSError:
        print("%s: not found, skipping" % (file_path,))
        return
    inode      = stat_res[stat.ST_INO]
    if not stat.S_ISREG(stat_res[stat.ST_MODE]):
        print("%s: not a file, skipping" % (file_path,))
        return
    old_inode  = None
    file_      = None
    with con:
        res = con.execute("""
            SELECT
                id,
                inode
            FROM
                file
            WHERE
                path=?;
        """, (file_path,)).fetchall()
        if res:
            file_     = res[0][0]
            old_inode = res[0][1]
    if old_inode != inode:
        with con:
            if file_ is None:
                cur = con.cursor()
                cur.execute("""
                    INSERT INTO
                        file(path, inode)
                    VALUES
                        (?, ?);
                """, (file_path, inode))
                file_ = cur.lastrowid
        inserts = []
        insert_count = 0
        if is_binary(file_path):
            if not update:
                print("%s: is binary, skipping" % (file_path,))
        else:
            pass_ = 0
            with open(file_path, "r") as f:
                while pass_ <= 2:
                    try:
                        f.seek(0)
                        lex = shlex.shlex(f, file_path)
                        ext = file_path.split(os.path.extsep)[-1]
                        apply_shlex_settings(
                            pass_,
                            ext,
                            lex
                        )
                        t = lex.get_token()
                        while t:
                            if insert_count % 1024 == 0:
                                # compress inserts
                                inserts = list(set(inserts))
                                # clear cache
                                if len(token_dict) > _cache_size:
                                    print("Clear cache")
                                    token_dict.clear()
                            insert_count += 1
                            word = cleanup(t)
                            inserts.append((
                                token_dict[word],
                                file_,
                                lex.lineno
                            ))
                            t = lex.get_token()
                    except ValueError:
                        if pass_ >= 2:
                            raise
                    pass_ += 1
            inserts        = list(set(inserts))
            unique_inserts = len(inserts)
            print("%s: indexed %s/%s (%.3f)" % (
                file_path,
                unique_inserts,
                insert_count,
                float(unique_inserts) / (insert_count + 0.0000000001)
            ))
        with con:
            con.execute("""
                DELETE FROM
                    finja
                WHERE
                    file_id=?;
            """, (file_,))
            con.executemany("""
                INSERT INTO
                    finja(token_id, file_id, line)
                VALUES
                    (?, ?, ?);
            """, inserts)
    else:
        if not update:
            print("%s: uptodate" % (file_path,))
        with con:
            con.execute("""
                UPDATE
                    file
                SET
                    inode = ?,
                    found = 1
                WHERE
                    id = ?
            """, (inode, file_))


def index():
    db = get_db(create=True)
    do_index(db)


def do_index(db, update=False):
    con = db[0]
    with con:
        con.execute("""
            UPDATE
                file
            SET found = 0
        """)
    for dirpath, _, filenames in os.walk("."):
        if set(dirpath.split(os.sep)).intersection(_ignore_dir):
            continue
        for filename in filenames:
            file_path = os.path.abspath(os.path.join(
                dirpath,
                filename
            ))
            index_file(db, file_path, update)
    with con:
        con.execute("""
            DELETE FROM
                finja
            WHERE
                file_id IN (
                    SELECT
                        id
                    FROM
                        file
                    WHERE
                        found = 0
                )
        """)
        con.execute("""
            DELETE FROM
                file
            WHERE
                found = 0
            """)
        con.execute("VACUUM;")


def find_finja():
    cwd = os.path.abspath(".")
    lcwd = cwd.split(os.sep)
    while lcwd:
        cwd = os.sep.join(lcwd)
        check = os.path.join(cwd, "FINJA")
        if os.path.isfile(check):
            return cwd
        lcwd.pop()
    raise ValueError("Could not find FINJA")


def gen_search_query(pignore, file_mode):
    base = """
        SELECT DISTINCT
            {projection}
        FROM
            finja as i
        JOIN
            file as f
        ON
            i.file_id = f.id
        WHERE
            token_id=?
        {ignore}
    """
    if file_mode:
        projection = """
            f.path,
        """
    else:
        projection = """
            f.path,
            i.line
        """
    ignore_list = []
    for ignore in pignore:
        ignore_list.append("AND f.path NOT LIKE ?")
    return base.format(
        projection = projection,
        ignore = "\n".join(ignore_list)
    )


def search(
        search,
        pignore,
        file_mode=False,
        update=False,
):
    finja = find_finja()
    os.chdir(finja)
    db = get_db(create=False)
    con = db[0]
    token_dict = db[1]
    if update:
        do_index(db, update=True)
    res = []
    with con:
        query = gen_search_query(pignore, file_mode)
        for word in search:
            word = cleanup(word)
            args = [token_dict[word]]
            args.extend(["%%%s%%" % x for x in pignore])
            res.append(set(con.execute(query, args).fetchall()))
    res_set = res.pop()
    for search_set in res:
        res_set.intersection_update(search_set)
    if file_mode:
        for match in sorted(
                res_set,
                reverse=True
        ):
            print(match[0])
    else:
        dirname = None
        for match in sorted(
                res_set,
                key=lambda x: (x[0], -x[1]),
                reverse=True
        ):
            path = match[0]
            if not _args.raw:
                new_dirname = os.path.dirname(path)
                if dirname != new_dirname:
                    dirname = new_dirname
                    print("%s:" % dirname)
                file_name = os.path.basename(path)
            else:
                file_name = path
            if path.startswith("./"):
                path = path[2:]
            context = _args.context
            if context == 1:
                print("%s:%5d:%s" % (
                    file_name,
                    match[1],
                    get_line(match[0], match[1])[:-1]
                ))
            else:
                offset = int(math.floor(context / 2))
                context_list = []
                for x in range(context):
                    x -= offset
                    context_list.append(
                        get_line(match[0], match[1] + x)
                    )
                strip_list = []
                inside = False
                for line in reversed(context_list):
                    if line.strip() or inside:
                        inside = True
                        strip_list.append(line)
                context_list = []
                inside = False
                for line in reversed(strip_list):
                    if line.strip() or inside:
                        inside = True
                        context_list.append(line)
                context = "|".join(context_list)
                print("%s:%5d\n|%s" % (
                    file_name,
                    match[1],
                    context
                ))


def main(argv=None):
    """Parse the args and excute"""
    global _args
    if not argv:  # pragma: no cover
        argv = sys.argv[1:]
    parser = argparse.ArgumentParser(description='Index and find stuff')
    parser.add_argument(
        '--index',
        '-i',
        help='Index the current directory',
        action='store_true',
    )
    parser.add_argument(
        '--update',
        '-u',
        help='Update the index before searching',
        action='store_true',
    )
    parser.add_argument(
        '--file-mode',
        '-f',
        help='Ignore line-number when matching search strings',
        action='store_true',
    )
    parser.add_argument(
        '--context',
        '-c',
        help='Lines of context. Default: 1',
        default=1,
        type=int
    )
    parser.add_argument(
        '--raw',
        '-r',
        help='Raw output to parse with outer tools',
        action='store_true',
    )
    parser.add_argument(
        '--pignore',
        '-p',
        help='Ignore path that contain one of the strings. Can be repeated',
        nargs='?',
        action='append'
    )
    parser.add_argument(
        'search',
        help='search string',
        type=str,
        nargs='*',
    )
    args = parser.parse_args(argv)
    _args = args  # noqa
    if args.index:
        index()
    if not args.pignore:
        args.pignore = []
    if args.search:
        search(
            args.search,
            args.pignore,
            file_mode=args.file_mode,
            update=args.update
        )
