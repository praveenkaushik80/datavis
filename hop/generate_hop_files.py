"""Generates the DataFusion Apache Hop project (pipelines, workflows, metadata).

Apache Hop is the operator front end: each Flow A / Flow B step is a workflow you run from Hop GUI or
Hop Web. Running a workflow opens a parameter dialog (the "form"), and the API response is shown in the
execution log. All workflows call one reusable pipeline, pipelines/call-datafusion-api.hpl.

Re-run after editing:  python generate_hop_files.py
"""
import json
from pathlib import Path
from xml.sax.saxutils import escape as x

ROOT = Path(__file__).parent / "datafusion"
STAMP = "2026/09/25 00:00:00.000"


def transform_common(name: str, ttype: str, xloc: int, yloc: int, body: str, desc: str = "") -> str:
    return f"""  <transform>
    <name>{x(name)}</name>
    <type>{ttype}</type>
    <description>{x(desc)}</description>
    <distribute>Y</distribute>
    <custom_distribution/>
    <copies>1</copies>
    <partitioning>
      <method>none</method>
      <schema_name/>
    </partitioning>
{body}
    <attributes/>
    <GUI>
      <xloc>{xloc}</xloc>
      <yloc>{yloc}</yloc>
    </GUI>
  </transform>
"""


def var_field(name: str, value: str) -> str:
    return f"""      <field>
        <name>{name}</name>
        <variable>{x(value)}</variable>
        <type>String</type>
        <format/>
        <currency/>
        <decimal/>
        <group/>
        <length>-1</length>
        <precision>-1</precision>
        <trim_type>none</trim_type>
      </field>"""


def rest_transform(xloc: int, app_type: str, body_field: str, params: list[tuple[str, str]] = ()) -> str:
    params_xml = "".join(f"""      <parameter>
        <field>{f}</field>
        <name>{n}</name>
      </parameter>
""" for n, f in params)
    return transform_common("Call DataFusion API", "Rest", xloc, 96, f"""    <applicationType>{app_type}</applicationType>
    <connection_name/>
    <url/>
    <urlInField>Y</urlInField>
    <urlField>url</urlField>
    <dynamicMethod>Y</dynamicMethod>
    <methodFieldName>method</methodFieldName>
    <method>POST</method>
    <bodyField>{body_field}</bodyField>
    <httpLogin/>
    <httpPassword>Encrypted </httpPassword>
    <proxyHost/>
    <proxyPort/>
    <trustStoreFile/>
    <trustStorePassword>Encrypted </trustStorePassword>
    <ignoreSsl>N</ignoreSsl>
    <connectionTimeout>15000</connectionTimeout>
    <readTimeout>${{DF_API_READ_TIMEOUT_MS}}</readTimeout>
    <headers>
      <header>
        <field>auth</field>
        <name>Authorization</name>
      </header>
    </headers>
    <parameters>
{params_xml}    </parameters>
    <matrixParameters>
    </matrixParameters>
    <result>
      <name>response</name>
      <code>http_status</code>
      <response_time>response_ms</response_time>
      <response_header/>
    </result>""", "Calls the DataFusion API with the Hop service token.")


def strip_transform(xloc: int, fields: list[str]) -> str:
    removes = "".join(f"""      <remove>
        <name>{f}</name>
      </remove>
""" for f in fields)
    return transform_common("Drop request secrets", "SelectValues", xloc, 96, f"""    <fields>
      <select_unspecified>N</select_unspecified>
{removes}    </fields>""", "Removes the request body / file content and the bearer token before anything is logged.")


def log_transform(name: str, xloc: int, yloc: int, fields: list[str]) -> str:
    fx = "".join(f"""      <field>
        <name>{f}</name>
      </field>
""" for f in fields)
    return transform_common(name, "WriteToLog", xloc, yloc, f"""    <loglevel>log_level_basic</loglevel>
    <displayHeader>Y</displayHeader>
    <limitRows>N</limitRows>
    <limitRowsNumber>0</limitRowsNumber>
    <logmessage>DataFusion API ${{API_PATH}}</logmessage>
    <fields>
{fx}    </fields>""")


ERROR_MARKER_CONDITION = """          <condition>
            <negated>Y</negated>
            <operator>AND</operator>
            <leftvalue>response</leftvalue>
            <function>CONTAINS</function>
            <rightvalue/>
            <value>
              <name>constant</name>
              <type>String</type>
              <text>datafusion_error</text>
              <length>-1</length>
              <precision>-1</precision>
              <isnull>N</isnull>
              <mask/>
            </value>
          </condition>
"""


def status_filter(xloc: int, true_to: str, false_to: str) -> str:
    """2xx status AND the body is not a keep-alive error ({"datafusion_error": true}, sent after a 200)."""
    cond = lambda op, fn, v: f"""          <condition>
            <negated>N</negated>{op}
            <leftvalue>http_status</leftvalue>
            <function>{fn}</function>
            <rightvalue/>
            <value>
              <name>constant</name>
              <type>Integer</type>
              <text>{v}</text>
              <length>-1</length>
              <precision>0</precision>
              <isnull>N</isnull>
              <mask>#</mask>
            </value>
          </condition>
"""
    return transform_common("HTTP 2xx?", "FilterRows", xloc, 96, f"""    <send_true_to>{x(true_to)}</send_true_to>
    <send_false_to>{x(false_to)}</send_false_to>
    <compare>
      <condition>
        <negated>N</negated>
        <conditions>
{cond("", "&gt;=", 200)}{cond(chr(10) + "            <operator>AND</operator>", "&lt;", 300)}{ERROR_MARKER_CONDITION}        </conditions>
      </condition>
    </compare>""", "Anything outside 200-299 fails the pipeline, so the workflow run shows as failed.")


def abort_transform(xloc: int, yloc: int) -> str:
    return transform_common("Fail with API error", "Abort", xloc, yloc, """    <abort_option>ABORT_WITH_ERROR</abort_option>
    <always_log_rows>N</always_log_rows>
    <row_threshold>0</row_threshold>
    <message>DataFusion API returned an error; see the response above. (Request body and token are never logged.)</message>""")


def pipeline_doc(name: str, description: str, params: list[tuple[str, str, str]], hops: list[tuple[str, str]],
                 transforms: str) -> str:
    hop_xml = "".join(f"""    <hop>
      <from>{x(a)}</from>
      <to>{x(b)}</to>
      <enabled>Y</enabled>
    </hop>
""" for a, b in hops)
    params_xml = "".join(f"""      <parameter>
        <name>{n}</name>
        <default_value>{x(d)}</default_value>
        <description>{x(desc)}</description>
      </parameter>
""" for n, d, desc in params)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<pipeline>
  <info>
    <name>{name}</name>
    <name_sync_with_filename>Y</name_sync_with_filename>
    <description>{x(description)}</description>
    <extended_description/>
    <pipeline_version/>
    <pipeline_type>Normal</pipeline_type>
    <parameters>
{params_xml}    </parameters>
    <capture_transform_performance>N</capture_transform_performance>
    <transform_performance_capturing_delay>1000</transform_performance_capturing_delay>
    <transform_performance_capturing_size_limit>100</transform_performance_capturing_size_limit>
    <created_user>-</created_user>
    <created_date>{STAMP}</created_date>
    <modified_user>-</modified_user>
    <modified_date>{STAMP}</modified_date>
  </info>
  <notepads>
  </notepads>
  <order>
{hop_xml}  </order>
{transforms}  <transform_error_handling>
  </transform_error_handling>
  <attributes/>
</pipeline>
"""


def post_file_pipeline() -> str:
    """Reads a local file and sends it as the raw request body (documents, edited review files)."""
    get_vars = transform_common("Build request", "GetVariable", 96, 96, f"""    <fields>
{var_field("filename", "${INPUT_FILE}")}
{var_field("url", "${DF_API_URL}${API_PATH}")}
{var_field("method", "POST")}
{var_field("auth", "Bearer ${API_TOKEN}")}
    </fields>""")
    load = transform_common("Read local file", "LoadFileInput", 288, 96, """    <include>N</include>
    <include_field/>
    <rownum>N</rownum>
    <addresultfile>N</addresultfile>
    <IsIgnoreEmptyFile>N</IsIgnoreEmptyFile>
    <IsIgnoreMissingPath>N</IsIgnoreMissingPath>
    <rownum_field/>
    <encoding/>
    <file>
      </file>
    <fields>
      <field>
        <name>content</name>
        <element_type>content</element_type>
        <type>Binary</type>
        <format/>
        <currency/>
        <decimal/>
        <group/>
        <length>-1</length>
        <precision>-1</precision>
        <trim_type>none</trim_type>
        <repeat>N</repeat>
      </field>
    </fields>
    <limit>0</limit>
    <IsInFields>Y</IsInFields>
    <DynamicFilenameField>filename</DynamicFilenameField>
    <shortFileFieldName>short_filename</shortFileFieldName>
    <pathFieldName/>
    <hiddenFieldName/>
    <lastModificationTimeFieldName/>
    <uriNameFieldName/>
    <rootUriNameFieldName/>
    <extensionFieldName/>""", "Loads the whole file (binary) so PDFs and Word files are sent unchanged.")
    t = (get_vars + load + rest_transform(480, "OCTET STREAM", "content", [("filename", "short_filename")])
         + strip_transform(672, ["content", "auth", "url"])
         + log_transform("Show response", 864, 96, ["short_filename", "http_status", "response_ms", "response"])
         + status_filter(1056, "Success", "Fail with API error")
         + transform_common("Success", "Dummy", 1248, 48, "") + abort_transform(1248, 160))
    return pipeline_doc("post-file-to-api", "Sends a local file as the request body.",
                        [("API_PATH", "/health", "Path under DF_API_URL"), ("INPUT_FILE", "", "Local file to send"),
                         ("API_TOKEN", "${DF_API_TOKEN}", "Bearer token")],
                        [("Build request", "Read local file"), ("Read local file", "Call DataFusion API"),
                         ("Call DataFusion API", "Drop request secrets"), ("Drop request secrets", "Show response"),
                         ("Show response", "HTTP 2xx?"), ("HTTP 2xx?", "Success"), ("HTTP 2xx?", "Fail with API error")],
                        t)


def save_response_pipeline() -> str:
    """GETs an API resource and writes the response body to a local file (review documents)."""
    get_vars = transform_common("Build request", "GetVariable", 96, 96, f"""    <fields>
{var_field("url", "${DF_API_URL}${API_PATH}")}
{var_field("method", "GET")}
{var_field("body", "{}")}
{var_field("auth", "Bearer ${API_TOKEN}")}
    </fields>""")
    write = transform_common("Write response to file", "TextFileOutput", 1056, 48, """    <schema_definition/>
    <separator/>
    <enclosure/>
    <enclosure_forced>N</enclosure_forced>
    <enclosure_fix_disabled>Y</enclosure_fix_disabled>
    <header>N</header>
    <footer>N</footer>
    <format>UNIX</format>
    <compression>None</compression>
    <encoding>UTF-8</encoding>
    <endedLine/>
    <fileNameInField>N</fileNameInField>
    <fileNameField/>
    <create_parent_folder>Y</create_parent_folder>
    <file>
      <name>${OUTPUT_FILE}</name>
      <servlet_output>N</servlet_output>
      <do_not_open_new_file_init>Y</do_not_open_new_file_init>
      <extention/>
      <append>N</append>
      <split>N</split>
      <haspartno>N</haspartno>
      <add_date>N</add_date>
      <add_time>N</add_time>
      <SpecifyFormat>N</SpecifyFormat>
      <date_time_format/>
      <add_to_result_filenames>N</add_to_result_filenames>
      <pad>N</pad>
      <fast_dump>Y</fast_dump>
      <splitevery/>
    </file>
    <fields>
      <field>
        <name>response</name>
        <type>String</type>
        <format/>
        <currency/>
        <decimal/>
        <group/>
        <nullif/>
        <trim_type>none</trim_type>
        <roundingType>half_even</roundingType>
        <length>-1</length>
        <precision>-1</precision>
      </field>
    </fields>""", "Saves the API response as ${OUTPUT_FILE}.")
    t = (get_vars + rest_transform(288, "JSON", "body") + strip_transform(480, ["body", "auth", "url"])
         + log_transform("Show status", 672, 96, ["http_status", "response_ms"])
         + status_filter(864, "Write response to file", "Show error")
         + write + log_transform("Show error", 1056, 160, ["response"]) + abort_transform(1248, 160))
    return pipeline_doc("save-api-response-to-file", "Saves an API response to a local file.",
                        [("API_PATH", "/health", "Path under DF_API_URL"), ("OUTPUT_FILE", "", "Local file to write"),
                         ("API_TOKEN", "${DF_API_TOKEN}", "Bearer token")],
                        [("Build request", "Call DataFusion API"), ("Call DataFusion API", "Drop request secrets"),
                         ("Drop request secrets", "Show status"), ("Show status", "HTTP 2xx?"),
                         ("HTTP 2xx?", "Write response to file"), ("HTTP 2xx?", "Show error"),
                         ("Show error", "Fail with API error")],
                        t)


def api_pipeline() -> str:
    get_vars = transform_common("Build request", "GetVariable", 96, 96, f"""    <fields>
{var_field("url", "${DF_API_URL}${API_PATH}")}
{var_field("method", "${HTTP_METHOD}")}
{var_field("body", "${BODY_JSON}")}
{var_field("auth", "Bearer ${API_TOKEN}")}
    </fields>""", "Turns the workflow parameters into one request row.")
    rest = transform_common("Call DataFusion API", "Rest", 288, 96, """    <applicationType>JSON</applicationType>
    <connection_name/>
    <url/>
    <urlInField>Y</urlInField>
    <urlField>url</urlField>
    <dynamicMethod>Y</dynamicMethod>
    <methodFieldName>method</methodFieldName>
    <method>POST</method>
    <bodyField>body</bodyField>
    <httpLogin/>
    <httpPassword>Encrypted </httpPassword>
    <proxyHost/>
    <proxyPort/>
    <trustStoreFile/>
    <trustStorePassword>Encrypted </trustStorePassword>
    <ignoreSsl>N</ignoreSsl>
    <connectionTimeout>15000</connectionTimeout>
    <readTimeout>${DF_API_READ_TIMEOUT_MS}</readTimeout>
    <headers>
      <header>
        <field>auth</field>
        <name>Authorization</name>
      </header>
    </headers>
    <parameters>
    </parameters>
    <matrixParameters>
    </matrixParameters>
    <result>
      <name>response</name>
      <code>http_status</code>
      <response_time>response_ms</response_time>
      <response_header/>
    </result>""", "Calls the DataFusion API with the Hop service token.")
    strip = transform_common("Drop request secrets", "SelectValues", 480, 96, """    <fields>
      <select_unspecified>N</select_unspecified>
      <remove>
        <name>body</name>
      </remove>
      <remove>
        <name>auth</name>
      </remove>
    </fields>""", "Removes the request body (may hold a password) and the bearer token before anything is logged.")
    log = transform_common("Show response", "WriteToLog", 672, 96, """    <loglevel>log_level_basic</loglevel>
    <displayHeader>Y</displayHeader>
    <limitRows>N</limitRows>
    <limitRowsNumber>0</limitRowsNumber>
    <logmessage>DataFusion API ${HTTP_METHOD} ${API_PATH}</logmessage>
    <fields>
      <field>
        <name>http_status</name>
      </field>
      <field>
        <name>response_ms</name>
      </field>
      <field>
        <name>response</name>
      </field>
    </fields>""")
    filt = status_filter(864, "Success", "Fail with API error")
    ok = transform_common("Success", "Dummy", 1056, 48, "")
    fail = transform_common("Fail with API error", "Abort", 1056, 160, """    <abort_option>ABORT_WITH_ERROR</abort_option>
    <always_log_rows>N</always_log_rows>
    <row_threshold>0</row_threshold>
    <message>DataFusion API returned an error; see the response above. (Request body and token are never logged.)</message>""")
    hops = "".join(f"""    <hop>
      <from>{x(a)}</from>
      <to>{x(b)}</to>
      <enabled>Y</enabled>
    </hop>
""" for a, b in [("Build request", "Call DataFusion API"), ("Call DataFusion API", "Drop request secrets"),
                 ("Drop request secrets", "Show response"),
                 ("Show response", "HTTP 2xx?"), ("HTTP 2xx?", "Success"), ("HTTP 2xx?", "Fail with API error")])
    params = "".join(f"""      <parameter>
        <name>{n}</name>
        <default_value>{x(d)}</default_value>
        <description>{x(desc)}</description>
      </parameter>
""" for n, d, desc in [("API_PATH", "/health", "Path under DF_API_URL"), ("HTTP_METHOD", "GET", "GET or POST"),
                       ("BODY_JSON", "{}", "JSON request body for POST"),
                       ("API_TOKEN", "${DF_API_TOKEN}", "Bearer token (steward by default)")])
    # Note: the static <method> must be a body method (POST) so the body field is wired up; the actual
    # verb comes from the "method" field (dynamic method), and GET requests are sent without a body.
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<pipeline>
  <info>
    <name>call-datafusion-api</name>
    <name_sync_with_filename>Y</name_sync_with_filename>
    <description>Reusable DataFusion API call used by every workflow.</description>
    <extended_description/>
    <pipeline_version/>
    <pipeline_type>Normal</pipeline_type>
    <parameters>
{params}    </parameters>
    <capture_transform_performance>N</capture_transform_performance>
    <transform_performance_capturing_delay>1000</transform_performance_capturing_delay>
    <transform_performance_capturing_size_limit>100</transform_performance_capturing_size_limit>
    <created_user>-</created_user>
    <created_date>{STAMP}</created_date>
    <modified_user>-</modified_user>
    <modified_date>{STAMP}</modified_date>
  </info>
  <notepads>
  </notepads>
  <order>
{hops}  </order>
{get_vars}{rest}{strip}{log}{filt}{ok}{fail}  <transform_error_handling>
  </transform_error_handling>
  <attributes/>
</pipeline>
"""


def workflow(name: str, description: str, params: list[tuple[str, str, str]], calls: list[tuple[str, str, str, str]],
             token: str = "${DF_API_TOKEN}") -> str:
    """calls: (action name, method, path, body json[, pipeline file, {extra parameters}])"""
    actions = [f"""    <action>
      <name>START</name>
      <description/>
      <type>SPECIAL</type>
      <attributes/>
      <repeat>N</repeat>
      <schedulerType>0</schedulerType>
      <intervalSeconds>0</intervalSeconds>
      <intervalMinutes>60</intervalMinutes>
      <hour>12</hour>
      <minutes>0</minutes>
      <weekDay>1</weekDay>
      <DayOfMonth>1</DayOfMonth>
      <parallel>N</parallel>
      <xloc>64</xloc>
      <yloc>96</yloc>
      <attributes_hac/>
    </action>
"""]
    hops, prev = [], "START"
    for i, call in enumerate(calls):
        aname, method, path, body = call[:4]
        pipe = call[4] if len(call) > 4 else "call-datafusion-api.hpl"
        extra = call[5] if len(call) > 5 else {}
        values = {"API_PATH": path, "API_TOKEN": token, **({"HTTP_METHOD": method, "BODY_JSON": body}
                                                             if pipe == "call-datafusion-api.hpl" else {}), **extra}
        param_values = "".join(f"""        <parameter>
          <name>{k}</name>
          <stream_name/>
          <value>{x(v)}</value>
        </parameter>
""" for k, v in values.items())
        actions.append(f"""    <action>
      <name>{x(aname)}</name>
      <description>{x(method + ' ' + path)}</description>
      <type>PIPELINE</type>
      <attributes/>
      <filename>${{PROJECT_HOME}}/pipelines/{pipe}</filename>
      <params_from_previous>N</params_from_previous>
      <exec_per_row>N</exec_per_row>
      <clear_rows>N</clear_rows>
      <clear_files>N</clear_files>
      <set_logfile>N</set_logfile>
      <logfile/>
      <logext/>
      <add_date>N</add_date>
      <add_time>N</add_time>
      <loglevel>Basic</loglevel>
      <set_append_logfile>N</set_append_logfile>
      <wait_until_finished>Y</wait_until_finished>
      <create_parent_folder>N</create_parent_folder>
      <run_configuration>local</run_configuration>
      <parameters>
{param_values}        <pass_all_parameters>N</pass_all_parameters>
      </parameters>
      <parallel>N</parallel>
      <xloc>{256 + i * 224}</xloc>
      <yloc>96</yloc>
      <attributes_hac/>
    </action>
""")
        hops.append((prev, aname, prev == "START"))
        prev = aname
    hop_xml = "".join(f"""    <hop>
      <from>{x(a)}</from>
      <to>{x(b)}</to>
      <enabled>Y</enabled>
      <evaluation>Y</evaluation>
      <unconditional>{'Y' if u else 'N'}</unconditional>
    </hop>
""" for a, b, u in hops)
    param_xml = "".join(f"""    <parameter>
      <name>{n}</name>
      <default_value>{x(d)}</default_value>
      <description>{x(desc)}</description>
    </parameter>
""" for n, d, desc in params)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<workflow>
  <name>{x(name)}</name>
  <name_sync_with_filename>Y</name_sync_with_filename>
  <description>{x(description)}</description>
  <extended_description/>
  <workflow_version/>
  <created_user>-</created_user>
  <created_date>{STAMP}</created_date>
  <modified_user>-</modified_user>
  <modified_date>{STAMP}</modified_date>
  <parameters>
{param_xml}  </parameters>
  <actions>
{''.join(actions)}  </actions>
  <hops>
{hop_xml}  </hops>
  <notepads>
    <notepad>
      <note>{x(description)}</note>
      <xloc>64</xloc>
      <yloc>192</yloc>
      <width>620</width>
      <heigth>60</heigth>
      <fontname/>
      <fontsize>-1</fontsize>
      <fontbold>N</fontbold>
      <fontitalic>N</fontitalic>
      <fontcolorred>14</fontcolorred>
      <fontcolorgreen>58</fontcolorgreen>
      <fontcolorblue>90</fontcolorblue>
      <backgroundcolorred>244</backgroundcolorred>
      <backgroundcolorgreen>246</backgroundcolorgreen>
      <backgroundcolorblue>248</backgroundcolorblue>
      <bordercolorred>196</bordercolorred>
      <bordercolorgreen>203</bordercolorgreen>
      <bordercolorblue>212</bordercolorblue>
    </notepad>
  </notepads>
  <attributes/>
</workflow>
"""


DB = ("DB_NAME", "sales", "DataFusion connection name (lowercase, e.g. sales)")
WORKFLOWS = {
    "flow-a/01-connect-database": (
        "Flow A, ribbon 1, steps 1-5: choose the database, enter credentials, verify, store credentials "
        "encrypted and run the metadata catalog pipeline automatically.",
        [DB, ("DB_TYPE", "postgres", "postgres (MVP1); mysql, mssql, oracle, snowflake (MVP2)"),
         ("DB_HOST", "client-db", "Host name"), ("DB_PORT", "5432", "Port"),
         ("DB_DATABASE", "shop", "Database name"), ("DB_USER", "datafusion_ro", "Read-only account"),
         ("DB_PASSWORD", "", "Password (sent once, stored encrypted, never logged)"),
         ("DB_OPTIONS", '{"schemas": ["public"]}', "JSON options: schemas, account, warehouse, role, service_name")],
        [("Verify the connection", "POST", "/api/connections/verify",
          '{"name":"${DB_NAME}","db_type":"${DB_TYPE}","host":"${DB_HOST}","port":${DB_PORT},'
          '"database":"${DB_DATABASE}","username":"${DB_USER}","password":"${DB_PASSWORD}","options":${DB_OPTIONS}}'),
         ("Store credentials and run catalog", "POST", "/api/connections",
          '{"name":"${DB_NAME}","db_type":"${DB_TYPE}","host":"${DB_HOST}","port":${DB_PORT},'
          '"database":"${DB_DATABASE}","username":"${DB_USER}","password":"${DB_PASSWORD}","options":${DB_OPTIONS},'
          '"run_catalog":true}')]),
    "flow-a/02-rerun-metadata-catalog": (
        "Re-run the metadata catalog pipeline (built-in extractor + OpenMetadata ingestion).",
        [DB], [("Run metadata catalog", "POST", "/api/connections/${DB_NAME}/catalog/run", "{}")]),
    "flow-a/03-browse-tables": (
        "Flow A, step 6: explorer of the connected database. Lists every table with its description.",
        [DB], [("List tables", "GET", "/api/connections/${DB_NAME}/tables", "{}")]),
    "flow-a/04-table-column-page": (
        "Flow A, step 7: see the metadata for one table.",
        [DB, ("TABLE", "public.orders", "schema.table")],
        [("Show table metadata", "GET", "/api/connections/${DB_NAME}/tables/${TABLE}", "{}")]),
    "flow-a/05-add-business-meaning": (
        "Flow A, step 7: edit a table or column description and add business meaning / requirements. "
        "Leave COLUMN empty to annotate the table. Avoid double quotes in text.",
        [DB, ("TABLE", "public.orders", "schema.table"), ("COLUMN", "", "Column name, or empty for the table"),
         ("DESCRIPTION", "", "Description"), ("BUSINESS_MEANING", "", "Business meaning and requirements")],
        [("Save business meaning", "POST", "/api/connections/${DB_NAME}/tables/${TABLE}/notes",
          '{"column":"${COLUMN}","description":"${DESCRIPTION}","business_meaning":"${BUSINESS_MEANING}"}')]),
    "flow-a/06-upload-document": (
        "Flow A, step 7: upload a supporting document (PDF, Word, JSON, TXT, CSV) from this machine. "
        "The file is sent over HTTPS, so this works with Docker and with the Vercel deployment.",
        [DB, ("FILE", "", "Local file path, e.g. /shared/docs/shop/business-rules.txt in Hop Web (Docker)"),
         ("TABLE", "", "Optional table the document is about")],
        [("Upload and parse document", "POST", "/api/connections/${DB_NAME}/documents/raw?table=${TABLE}", "",
          "post-file-to-api.hpl", {"INPUT_FILE": "${FILE}"})]),
    "flow-a/07-run-context-research": (
        "Flow A, steps 8-9: the context research agent analyses metadata, notes and documents (and the web "
        "when ON) and creates draft metadata and knowledge base layers. Writes a review file.",
        [DB, ("WEB_SEARCH", "false", "true to allow web search (business terms only)")],
        [("Run context research agent", "POST", "/api/connections/${DB_NAME}/research",
          '{"web_search":${WEB_SEARCH}}')]),
    "flow-a/08-review-draft": (
        "Flow A, step 10: save a version's editable review document to a local JSON file. Edit descriptions, "
        "business meaning, PII flags, glossary and rules in that file, then run 09-apply-review-edits.",
        [DB, ("VERSION", "latest", "Version number or latest"),
         ("REVIEW_FILE", "${PROJECT_HOME}/review/${DB_NAME}-context-${VERSION}.json", "Local file to write")],
        [("Save review document", "GET", "/api/connections/${DB_NAME}/versions/${VERSION}/review", "",
          "save-api-response-to-file.hpl", {"OUTPUT_FILE": "${REVIEW_FILE}"}),
         ("Show version summary", "GET", "/api/connections/${DB_NAME}/versions/${VERSION}", "{}")]),
    "flow-a/09-apply-review-edits": (
        "Flow A, step 10: send your edited review file back; the draft version is updated.",
        [DB, ("VERSION", "latest", "Draft version number or latest"),
         ("REVIEW_FILE", "${PROJECT_HOME}/review/${DB_NAME}-context-${VERSION}.json", "Edited review file")],
        [("Apply review edits", "POST", "/api/connections/${DB_NAME}/versions/${VERSION}/edits/raw", "",
          "post-file-to-api.hpl", {"INPUT_FILE": "${REVIEW_FILE}"})]),
    "flow-a/10-approve-context": (
        "Flow A, step 11: approve the draft. Publishes the semantic context layer (Context API), generates the "
        "Cube data model and writes descriptions and glossary to OpenMetadata.",
        [DB, ("VERSION", "1", "Draft version number"), ("NOTE", "", "Approval note")],
        [("Approve semantic context", "POST", "/api/connections/${DB_NAME}/versions/${VERSION}/approve",
          '{"note":"${NOTE}"}')]),
    "flow-a/11-reject-context": (
        "Flow A, step 10: reject a draft. It is kept for the record.",
        [DB, ("VERSION", "1", "Draft version number"), ("NOTE", "", "Reason")],
        [("Reject draft", "POST", "/api/connections/${DB_NAME}/versions/${VERSION}/reject",
          '{"note":"${NOTE}"}')]),
    "flow-a/12-list-versions": (
        "Every version of the semantic context, with status and who decided.",
        [DB], [("List versions", "GET", "/api/connections/${DB_NAME}/versions", "{}")]),
    "flow-b/20-ask-question": (
        "Flow B: ask a question in natural language. The agent uses the approved semantic context and the "
        "auto-generated MCP servers; the answer includes sources and confidence.",
        [("QUESTION", "How many orders were paid last month?", "Your question (avoid double quotes)"),
         ("DB_NAME", "", "Optional: restrict to one connection")],
        [("Ask the agent", "POST", "/api/ask", '{"question":"${QUESTION}","connection":"${DB_NAME}"}')]),
    "flow-b/21-context-api": (
        "Flow B: the approved semantic context as served by the Context API.",
        [DB], [("Get approved context", "GET", "/api/context/${DB_NAME}", "{}")]),
    "admin/30-add-user": (
        "Admin: add or update a user (role user, steward or admin).",
        [("EMAIL", "", "User email"), ("DEPARTMENT", "", "Department"), ("ROLE", "user", "user | steward | admin")],
        [("Save user", "POST", "/api/admin/users",
          '{"email":"${EMAIL}","department":"${DEPARTMENT}","role":"${ROLE}"}')]),
    "admin/31-grant-read-access": (
        "Admin: grant read access for a department or user to a database, optionally limited to tables and with "
        "restricted columns.",
        [("SUBJECT_TYPE", "department", "department | user"), ("SUBJECT", "", "Department name or email"), DB,
         ("TABLES", "[]", 'JSON list, e.g. ["public.orders"]; [] = all tables'),
         ("DENIED_COLUMNS", "[]", 'JSON list, e.g. ["public.customers.email"]')],
        [("Grant read access", "POST", "/api/admin/permissions",
          '{"subject_type":"${SUBJECT_TYPE}","subject":"${SUBJECT}","connection":"${DB_NAME}",'
          '"tables":${TABLES},"denied_columns":${DENIED_COLUMNS}}')]),
    "admin/32-list-permissions": ("Admin: list all read-access grants.", [],
                                  [("List permissions", "GET", "/api/admin/permissions", "{}")]),
    "observability/40-usage-summary": (
        "Observability: questions, agent executions, token and LLM usage, databases and MCP tools used, errors.",
        [("HOURS", "24", "Time window in hours")],
        [("Usage summary", "GET", "/api/observability/summary?hours=${HOURS}", "{}")]),
    "observability/41-recent-events": (
        "Observability: most recent events (API calls, agent traces, tool calls, errors).",
        [("LIMIT", "50", "Number of events"), ("KIND", "", "api | agent | tool | llm | error, or empty")],
        [("Recent events", "GET", "/api/observability/events?limit=${LIMIT}&kind=${KIND}", "{}")]),
    "00-check-api": ("Check that Hop can reach the DataFusion API.", [],
                     [("Health check", "GET", "/health", "{}"), ("Who am I", "GET", "/api/me", "{}")]),
}


def main() -> None:
    (ROOT / "pipelines").mkdir(parents=True, exist_ok=True)
    (ROOT / "pipelines" / "call-datafusion-api.hpl").write_text(api_pipeline())
    (ROOT / "pipelines" / "post-file-to-api.hpl").write_text(post_file_pipeline())
    (ROOT / "pipelines" / "save-api-response-to-file.hpl").write_text(save_response_pipeline())
    (ROOT / "review").mkdir(exist_ok=True)
    (ROOT / "review" / ".gitkeep").write_text("")
    for rel, (desc, params, calls) in WORKFLOWS.items():
        p = ROOT / "workflows" / f"{rel}.hwf"
        p.parent.mkdir(parents=True, exist_ok=True)
        token = "${DF_ADMIN_TOKEN}" if rel.startswith("admin/") else "${DF_API_TOKEN}"
        p.write_text(workflow(p.stem, desc, params, calls, token))
    meta = ROOT / "metadata"
    for kind, cfg in {
        "pipeline-run-configuration": {"engineRunConfiguration": {"Local": {
            "feedback_size": "50000", "sample_size": "100", "sample_type_in_gui": "Last", "rowset_size": "10000",
            "safe_mode": False, "show_feedback": False, "topo_sort": False, "gather_metrics": False}},
            "name": "local", "configurationVariables": [], "description": "", "defaultSelection": True},
        "workflow-run-configuration": {"engineRunConfiguration": {"Local": {"safe_mode": False}},
                                       "name": "local", "description": "", "defaultSelection": True},
    }.items():
        (meta / kind).mkdir(parents=True, exist_ok=True)
        (meta / kind / "local.json").write_text(json.dumps(cfg, indent=2))
    (ROOT / "project-config.json").write_text(json.dumps({
        "metadataBaseFolder": "${PROJECT_HOME}/metadata", "unitTestsBasePath": "${PROJECT_HOME}",
        "dataSetsCsvFolder": "${PROJECT_HOME}/datasets", "enforcingExecutionInHome": True,
        "parentProjectName": "", "config": {"variables": []}}, indent=2))
    env_dir = Path(__file__).parent / "environments"
    env_dir.mkdir(exist_ok=True)
    (env_dir / "datafusion-docker.json").write_text(json.dumps({"variables": [
        {"name": "DF_API_URL", "value": "http://backend:8000", "description": "DataFusion API base URL"},
        {"name": "DF_API_TOKEN", "value": "${HOP_API_TOKEN}", "description": "Steward service token (from the environment)"},
        {"name": "DF_ADMIN_TOKEN", "value": "${HOP_ADMIN_TOKEN}", "description": "Admin token for admin workflows"},
        {"name": "DF_API_READ_TIMEOUT_MS", "value": "900000", "description": "Research can take several minutes"},
    ]}, indent=2))
    # Remote API (e.g. Vercel): Hop GUI on a laptop or Hop Web on any host. Values come from Java system
    # properties, e.g. HOP_OPTIONS="-DDATAFUSION_API_URL=https://x.vercel.app -DHOP_API_TOKEN=..."
    (env_dir / "datafusion-remote.json").write_text(json.dumps({"variables": [
        {"name": "DF_API_URL", "value": "${DATAFUSION_API_URL}", "description": "e.g. https://datafusion.vercel.app"},
        {"name": "DF_API_TOKEN", "value": "${HOP_API_TOKEN}", "description": "Steward service token"},
        {"name": "DF_ADMIN_TOKEN", "value": "${HOP_ADMIN_TOKEN}", "description": "Admin token for admin workflows"},
        {"name": "DF_API_READ_TIMEOUT_MS", "value": "320000", "description": "Vercel functions stop at 300 s (Hobby)"},
    ]}, indent=2))
    # Hop Web running inside Railway, calling the API over the private network (no request time limit)
    (env_dir / "datafusion-railway.json").write_text(json.dumps({"variables": [
        {"name": "DF_API_URL", "value": "${DATAFUSION_API_URL}", "description": "http://<backend>.railway.internal:8000"},
        {"name": "DF_API_TOKEN", "value": "${HOP_API_TOKEN}", "description": "Steward service token"},
        {"name": "DF_ADMIN_TOKEN", "value": "${HOP_ADMIN_TOKEN}", "description": "Admin token for admin workflows"},
        {"name": "DF_API_READ_TIMEOUT_MS", "value": "900000", "description": "Research can take several minutes"},
    ]}, indent=2))
    print(f"wrote {len(WORKFLOWS)} workflows")


if __name__ == "__main__":
    main()
