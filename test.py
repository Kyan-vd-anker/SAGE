import time

class RocketMotor:
    def __init__(self, thrust, burn_time):
        self.thrust = thrust  # Thrust in Newtons
        self.burn_time = burn_time  # Burn time in seconds
        self.current_time = 0
        self.velocity = 0
        self.altitude = 0

    def update_state(self, dt):
        if self.current_time < self.burn_time:
            self.velocity += self.thrust * dt / 1000  # Assuming mass is constant for simplicity
            self.altitude += self.velocity * dt
            self.current_time += dt
        else:
            self.current_time = self.burn_time

    def simulate(self, time_step=0.1):
        while self.current_time < self.burn_time:
            self.update_state(time_step)
            print(f"Time: {self.current_time:.2f}s, Velocity: {self.velocity:.2f}m/s, Altitude: {self.altitude:.2f}m")
            time.sleep(time_step / 1000)

# Example usage
rocket = RocketMotor(thrust=5000, burn_time=10)
rocket.simulate()

