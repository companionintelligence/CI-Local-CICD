#!/usr/bin/env python3
"""
Deploy CI-Server monitoring daemon as a system service
"""

import os
import sys
import subprocess
import pwd
import stat

def create_systemd_service():
    """Create systemd service file for the CI monitoring daemon"""
    user = pwd.getpwuid(os.getuid()).pw_name
    home_dir = os.path.expanduser("~")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    service_content = f"""[Unit]
Description=CI-Server Local CI/CD Monitor
After=network.target
Wants=network-online.target

[Service]
Type=simple
User={user}
WorkingDirectory={script_dir}
ExecStart=/usr/bin/python3 {script_dir}/ci-server_local_cicd.py
Restart=always
RestartSec=10
Environment=HOME={home_dir}
Environment=PATH=/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
"""
    
    service_file = "/tmp/ci-server-monitor.service"
    with open(service_file, 'w') as f:
        f.write(service_content)
    
    return service_file

def deploy_service():
    """Deploy the monitoring service"""
    print("🚀 Deploying CI-Server monitoring daemon...")
    
    # Create service file
    service_file = create_systemd_service()
    print(f"✅ Created service file: {service_file}")
    
    try:
        # Copy service file to systemd directory (requires sudo)
        print("📋 Installing systemd service (requires sudo)...")
        subprocess.run([
            "sudo", "cp", service_file, "/etc/systemd/system/ci-server-monitor.service"
        ], check=True)
        
        # Reload systemd
        print("🔄 Reloading systemd...")
        subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
        
        # Enable service
        print("⚡ Enabling service...")
        subprocess.run(["sudo", "systemctl", "enable", "ci-server-monitor.service"], check=True)
        
        # Start service
        print("🚀 Starting service...")
        subprocess.run(["sudo", "systemctl", "start", "ci-server-monitor.service"], check=True)
        
        print("\n✅ CI-Server monitoring deployed successfully!")
        print("\n📊 Service commands:")
        print("  Check status:  sudo systemctl status ci-server-monitor")
        print("  View logs:     sudo journalctl -u ci-server-monitor -f")
        print("  Stop service:  sudo systemctl stop ci-server-monitor")
        print("  Restart:       sudo systemctl restart ci-server-monitor")
        
    except subprocess.CalledProcessError as e:
        print(f"❌ Error deploying service: {e}")
        return False
    
    return True

def create_manual_runner():
    """Create a script to run the daemon manually (alternative to systemd)"""
    script_content = """#!/bin/bash

# Manual CI-Server Monitor Runner
cd "$(dirname "$0")"

echo "🚀 Starting CI-Server monitoring daemon..."
echo "Press Ctrl+C to stop"
echo "Logs will be written to ci-monitor.log"

python3 ci-server_local_cicd.py 2>&1 | tee -a ci-monitor.log
"""
    
    runner_file = "/home/liam/devel/local-cicd/run_ci_monitor.sh"
    with open(runner_file, 'w') as f:
        f.write(script_content)
    
    # Make executable
    os.chmod(runner_file, stat.S_IRWXU | stat.S_IRGRP | stat.S_IROTH)
    
    print(f"✅ Created manual runner: {runner_file}")
    return runner_file

if __name__ == "__main__":
    print("=== CI-Server Monitor Deployment ===")
    
    # Check if CI-Server repo exists
    repo_path = os.path.expanduser("~/devel/CI-Server")
    if not os.path.exists(repo_path):
        print("❌ CI-Server repository not found!")
        print("Please run the test script first to verify setup.")
        sys.exit(1)
    
    print(f"✅ Found CI-Server repository at {repo_path}")
    
    # Ask deployment preference
    print("\nDeployment options:")
    print("1. Systemd service (runs automatically on boot)")
    print("2. Manual script (run when needed)")
    
    choice = input("\nChoose deployment method (1/2): ").strip()
    
    if choice == "1":
        if deploy_service():
            print("\n🎉 Systemd service deployed successfully!")
        else:
            print("\n⚠️  Systemd deployment failed, creating manual runner...")
            create_manual_runner()
    else:
        runner_file = create_manual_runner()
        print(f"\n✅ Manual runner created: {runner_file}")
        print("To start monitoring, run: ./run_ci_monitor.sh")