#!/usr/bin/env python3
"""
Test script to run the CI-Server local CI/CD daemon
"""

import sys
import os
import subprocess
import signal

def check_repo_exists():
    """Check if the CI-Server repository exists"""
    repo_path = os.path.expanduser("~/devel/CI-Server")
    if not os.path.exists(repo_path):
        print(f"❌ Repository not found at {repo_path}")
        print("Please clone the repository first:")
        print("  cd ~/devel")
        print("  git clone https://github.com/companionintelligence/CI-Server")
        return False
    
    if not os.path.exists(os.path.join(repo_path, ".git")):
        print(f"❌ {repo_path} is not a Git repository")
        return False
    
    print(f"✅ Found CI-Server repository at {repo_path}")
    return True

def check_repo_status():
    """Check the current status of the repository"""
    repo_path = os.path.expanduser("~/devel/CI-Server")
    os.chdir(repo_path)
    
    # Get current branch
    result = subprocess.run(["git", "branch", "--show-current"], 
                          capture_output=True, text=True)
    current_branch = result.stdout.strip()
    print(f"Current branch: {current_branch}")
    
    # Get current commit
    result = subprocess.run(["git", "rev-parse", "HEAD"], 
                          capture_output=True, text=True)
    current_commit = result.stdout.strip()[:8]
    print(f"Current commit: {current_commit}")
    
    # Check remote status
    subprocess.run(["git", "fetch"], capture_output=True)
    result = subprocess.run(["git", "rev-parse", "origin/main"], 
                          capture_output=True, text=True)
    remote_commit = result.stdout.strip()[:8]
    print(f"Remote commit: {remote_commit}")
    
    if current_commit != remote_commit:
        print("⚠️  Local repository is behind remote")
    else:
        print("✅ Local repository is up to date")

def run_daemon():
    """Run the CI-Server daemon"""
    print("\n🚀 Starting CI-Server monitoring daemon...")
    print("Press Ctrl+C to stop the daemon")
    
    try:
        # Import and run the daemon
        sys.path.insert(0, os.path.dirname(__file__))
        import importlib.util
        spec = importlib.util.spec_from_file_location("ci_server_daemon", "ci-server_local_cicd.py")
        daemon_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(daemon_module)
        daemon_module.main()
    except KeyboardInterrupt:
        print("\n🛑 Daemon stopped by user")
    except Exception as e:
        print(f"\n❌ Error running daemon: {e}")

if __name__ == "__main__":
    print("=== CI-Server Local CI/CD Test ===")
    
    # Check if repository exists
    if not check_repo_exists():
        sys.exit(1)
    
    # Check repository status
    print("\n📊 Repository Status:")
    check_repo_status()
    
    # Ask user if they want to proceed
    print("\nThis will start monitoring ~/devel/CI-Server for changes.")
    print("The daemon will check for updates every 5 minutes.")
    response = input("\nStart the daemon? (y/N): ").lower()
    
    if response == 'y':
        run_daemon()
    else:
        print("Test cancelled.")