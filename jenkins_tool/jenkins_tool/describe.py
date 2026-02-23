"""Tool self-description for Jenkins CLI."""

from llm_tool_common.describe import Argument, Command, ToolDescription


def get_tool_description() -> ToolDescription:
    return ToolDescription(
        name="jenkins",
        version="0.1.0",
        description="LLM-agent-focused CLI for Jenkins build server. "
                    "Query build status, console output, and Gerrit review builds.",
        env_vars=[
            {"name": "JENKINS_URL", "description": "Jenkins server URL (default: https://build.whamcloud.com)"},
            {"name": "JENKINS_USER", "description": "Jenkins username"},
            {"name": "JENKINS_TOKEN", "description": "Jenkins API token"},
        ],
        commands=[
            Command(
                name="jobs",
                description="List all jobs with status and health",
                usage="jenkins jobs [--view NAME]",
                arguments=[
                    Argument(name="--view", description="Filter by view name"),
                ],
                examples=[
                    "jenkins jobs",
                    "jenkins jobs --view lustre",
                ],
                output_fields=["count", "jobs[name,status,url,health_score,health]"],
                next_actions=["jenkins builds <name>"],
            ),
            Command(
                name="builds",
                description="List recent builds for a job",
                usage="jenkins builds <JOB_NAME> [--limit N]",
                arguments=[
                    Argument(name="job_name", description="Job name", required=True),
                    Argument(name="--limit", description="Number of builds", type="integer", default=10),
                ],
                examples=[
                    "jenkins builds lustre-master",
                    "jenkins builds lustre-reviews --limit 20",
                ],
                output_fields=["job", "count", "builds[number,result,building,timestamp,duration]"],
                next_actions=["jenkins build <name> <number>"],
            ),
            Command(
                name="build",
                description="Show details for a specific build including parameters, Gerrit info, "
                            "and matrix sub-builds (runs) with per-configuration status",
                usage="jenkins build <JOB_NAME> [BUILD_NUMBER]",
                arguments=[
                    Argument(name="job_name", description="Job name", required=True),
                    Argument(name="build_number", description="Build number or lastBuild/lastFailedBuild",
                             default="lastBuild"),
                ],
                examples=[
                    "jenkins build lustre-master 4704",
                    "jenkins build lustre-reviews lastBuild",
                    "jenkins build lustre-master lastFailedBuild",
                ],
                output_fields=[
                    "number", "result", "building", "timestamp", "duration",
                    "causes", "parameters", "gerrit", "commits",
                    "runs_total", "runs_failed", "runs_building", "runs_success",
                    "runs[config,result,building,duration,node,url]",
                ],
                next_actions=[
                    "jenkins console <name> <number>",
                    "jenkins run-console <name> <number> <config>",
                    "jenkins review <change>",
                    "jenkins retrigger <name> <number>",
                ],
            ),
            Command(
                name="console",
                description="Get console output for a build (tail by default)",
                usage="jenkins console <JOB_NAME> [BUILD_NUMBER] [--tail N] [--grep PATTERN]",
                arguments=[
                    Argument(name="job_name", description="Job name", required=True),
                    Argument(name="build_number", description="Build number", default="lastBuild"),
                    Argument(name="--tail", description="Lines from end", type="integer", default=200),
                    Argument(name="--head", description="Lines from start", type="integer"),
                    Argument(name="--grep", description="Filter lines by regex pattern"),
                ],
                examples=[
                    "jenkins console lustre-master 4704",
                    "jenkins console lustre-reviews lastBuild --tail 50",
                    "jenkins console lustre-master lastFailedBuild --grep error",
                ],
                output_fields=["job", "build", "total_lines", "showing", "lines"],
                next_actions=["jenkins build <name> <number>"],
            ),
            Command(
                name="review",
                description="Find builds for a Gerrit review change number",
                usage="jenkins review <CHANGE_NUMBER> [--job NAME] [--limit N]",
                arguments=[
                    Argument(name="change_number", description="Gerrit change number", type="integer", required=True),
                    Argument(name="--job", description="Specific job to search"),
                    Argument(name="--limit", description="Max builds to search per job", type="integer", default=20),
                ],
                examples=[
                    "jenkins review 54225",
                    "jenkins review 54225 --job lustre-reviews",
                ],
                output_fields=["change_number", "count", "builds[number,result,gerrit]"],
                next_actions=["jenkins console <job> <number>"],
            ),
            Command(
                name="run-console",
                description="Get console output for a specific matrix sub-build (run)",
                usage='jenkins run-console <JOB_NAME> <BUILD_NUMBER> "<CONFIG>"',
                arguments=[
                    Argument(name="job_name", description="Job name", required=True),
                    Argument(name="build_number", description="Build number", type="integer", required=True),
                    Argument(name="config", description="Matrix config string from 'jenkins build' output",
                             required=True),
                    Argument(name="--tail", description="Lines from end", type="integer", default=200),
                    Argument(name="--head", description="Lines from start", type="integer"),
                    Argument(name="--grep", description="Filter lines by regex pattern"),
                ],
                examples=[
                    'jenkins run-console lustre-reviews 121880 '
                    '"arch=x86_64,build_type=client,distro=el8.9,ib_stack=inkernel"',
                    'jenkins run-console lustre-reviews 121880 '
                    '"arch=x86_64,build_type=client,distro=el8.9,ib_stack=inkernel" --grep "error"',
                ],
                output_fields=["job", "build", "config", "total_lines", "showing", "lines"],
                next_actions=["jenkins build <name> <number>"],
            ),
            Command(
                name="abort",
                description="Abort a running build and all its sub-builds",
                usage="jenkins abort <JOB_NAME> <BUILD_NUMBER> [--kill]",
                arguments=[
                    Argument(name="job_name", description="Job name", required=True),
                    Argument(name="build_number", description="Build number to abort",
                             type="integer", required=True),
                    Argument(name="--kill", description="Hard-kill instead of graceful stop",
                             type="boolean", default=False),
                ],
                examples=[
                    "jenkins abort lustre-reviews 121884",
                    "jenkins abort lustre-reviews 121884 --kill",
                ],
                output_fields=["job", "build", "action", "aborted", "message", "sub_builds_stopped"],
                next_actions=["jenkins build <name> <number>"],
            ),
            Command(
                name="retrigger",
                description="Retrigger a Gerrit-triggered build with same event parameters",
                usage="jenkins retrigger <JOB_NAME> <BUILD_NUMBER>",
                arguments=[
                    Argument(name="job_name", description="Job name", required=True),
                    Argument(name="build_number", description="Build number to retrigger",
                             type="integer", required=True),
                ],
                examples=[
                    "jenkins retrigger lustre-reviews 121880",
                    "jenkins retrigger lustre-master 4699",
                ],
                output_fields=["success", "job", "original_build", "message", "redirect"],
                next_actions=["jenkins builds <name>"],
            ),
        ],
    )
